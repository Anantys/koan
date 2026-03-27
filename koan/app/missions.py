#!/usr/bin/env python3
"""
Kōan — Centralized missions.md parser

Single source of truth for parsing, querying, and modifying missions.md.
All modules that interact with missions.md should use these functions
instead of reimplementing section detection and parsing.
"""

import re
import time
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple


# Section name normalization — accepts French and English variants
_SECTION_MAP = {
    "en attente": "pending",
    "pending": "pending",
    "en cours": "in_progress",
    "in progress": "in_progress",
    "in_progress": "in_progress",
    "terminées": "done",
    "terminés": "done",
    "done": "done",
    "completed": "done",
    "failed": "failed",
}

DEFAULT_SKELETON = "# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n\n## Failed\n"

# Timestamp markers for mission lifecycle tracking
_QUEUED_MARKER = "⏳"
_STARTED_MARKER = "▶"
_QUEUED_PATTERN = re.compile(
    r"\s*⏳\((\d{4}-\d{2}-\d{2}T\d{2}:\d{2})\)"
)
_STARTED_PATTERN = re.compile(
    r"\s*▶\((\d{4}-\d{2}-\d{2}T\d{2}:\d{2})\)"
)
_COMPLETED_PATTERN = re.compile(
    r"\s*[✅❌]\s*\((\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\)"
)
_TS_FORMAT = "%Y-%m-%dT%H:%M"


def _now_iso() -> str:
    """Return current time as YYYY-MM-DDTHH:MM."""
    return time.strftime(_TS_FORMAT)


def stamp_queued(entry: str) -> str:
    """Append a queued timestamp to a mission entry."""
    return f"{entry} {_QUEUED_MARKER}({_now_iso()})"


def stamp_started(entry: str) -> str:
    """Append a started timestamp to a mission entry line."""
    return f"{entry} {_STARTED_MARKER}({_now_iso()})"


def sanitize_mission_text(text: str) -> str:
    """Sanitize user-submitted mission text for safe embedding in missions.md.

    Collapses newlines and carriage returns into spaces and strips leading/trailing
    whitespace so a multi-line Telegram message becomes a single markdown list item.
    """
    # Replace \r\n, \r, \n with a single space
    text = re.sub(r"\r\n|\r|\n", " ", text)
    # Collapse multiple consecutive spaces into one
    text = re.sub(r"  +", " ", text)
    return text.strip()


def _parse_ts(pattern: re.Pattern, text: str, fmt: str = _TS_FORMAT) -> Optional[datetime]:
    """Parse a single timestamp from *text* using *pattern*, or return None."""
    match = pattern.search(text)
    if not match:
        return None
    raw = "T".join(match.groups()) if len(match.groups()) > 1 else match.group(1)
    try:
        return datetime.strptime(raw, fmt)
    except ValueError:
        return None


def extract_timestamps(text: str) -> Dict[str, Optional[datetime]]:
    """Extract lifecycle timestamps from a mission line.

    Returns {"queued": datetime|None, "started": datetime|None, "completed": datetime|None}.
    """
    return {
        "queued": _parse_ts(_QUEUED_PATTERN, text),
        "started": _parse_ts(_STARTED_PATTERN, text),
        "completed": _parse_ts(_COMPLETED_PATTERN, text),
    }


def format_duration(seconds: float) -> str:
    """Format a duration in seconds as a human-friendly string.

    Examples: "2m", "1h 30m", "3h", "< 1m".
    Returns "< 1m" for zero or negative values.
    """
    if seconds < 60:
        return "< 1m"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    remaining = minutes % 60
    if remaining == 0:
        return f"{hours}h"
    return f"{hours}h {remaining}m"


def mission_timing_display(text: str) -> str:
    """Return a short timing summary for a mission line.

    Examples: "waiting 5m", "running 12m", "took 1h 30m, waited 5m".
    Returns empty string if no timestamps found.
    All timestamps are timezone-naive (system local time).
    """
    ts = extract_timestamps(text)

    # Completed mission: show execution time and optional wait time
    if ts["completed"] and ts["started"]:
        duration = (ts["completed"] - ts["started"]).total_seconds()
        if duration < 0:
            return ""
        parts = [f"took {format_duration(duration)}"]

        if ts["queued"]:
            wait = (ts["started"] - ts["queued"]).total_seconds()
            if wait >= 60:
                parts.append(f"waited {format_duration(wait)}")

        return ", ".join(parts)

    # In-progress mission: show running time
    if ts["started"]:
        elapsed = (datetime.now() - ts["started"]).total_seconds()
        if elapsed < 0:
            return ""
        return f"running {format_duration(elapsed)}"

    # Queued mission: show waiting time
    if ts["queued"]:
        elapsed = (datetime.now() - ts["queued"]).total_seconds()
        if elapsed < 0:
            return ""
        return f"waiting {format_duration(elapsed)}"

    return ""


def strip_timestamps(text: str) -> str:
    """Remove queued/started timestamp markers from mission text.

    Preserves completion markers (✅/❌) — those are part of the Done/Failed format.
    Useful for clean display when timestamps are shown separately.
    """
    text = _QUEUED_PATTERN.sub("", text)
    text = _STARTED_PATTERN.sub("", text)
    return text.rstrip()


def _normalize_now_flag(text: str) -> str:
    """Normalize Unicode dash variants of --now to ASCII --now.

    Mobile keyboards often auto-correct -- to em dash (\u2014) or en dash (\u2013),
    so "\u2014now" and "\u2013now" should be treated as "--now".
    """
    return text.replace("\u2014now", "--now").replace("\u2013now", "--now")


def extract_now_flag(text: str) -> Tuple[bool, str]:
    """Check for --now flag in the first 5 words of mission text.

    Returns (is_urgent, cleaned_text) where cleaned_text has --now removed.
    Accepts Unicode dash variants (\u2014now, \u2013now) as synonyms for --now.
    """
    text = _normalize_now_flag(text)
    words = text.split()
    first_five = words[:5]
    if "--now" in first_five:
        words_copy = list(words)
        # Remove the first occurrence of --now from the full word list
        words_copy.remove("--now")
        return True, " ".join(words_copy)
    return False, text


def classify_section(header_text: str) -> Optional[str]:
    """Normalize a ## header into a canonical section key.

    Returns "pending", "in_progress", "done", or None if unrecognized.
    """
    return _SECTION_MAP.get(header_text.strip().lower())


def parse_sections(content: str) -> Dict[str, List[str]]:
    """Parse missions.md content into structured sections.

    Returns {"pending": [...], "in_progress": [...], "done": [...]}.
    Each item is either a simple "- ..." line or a multi-line block
    (for ### complex missions). Continuation lines (indented text,
    code-fenced blocks) are attached to their parent "- ..." item.
    """
    sections = {"pending": [], "in_progress": [], "done": [], "failed": []}
    current = None
    current_block = []
    in_code_fence = False

    for line in content.splitlines():
        stripped = line.strip()

        # Track code fences — content inside fences is never structural
        if stripped.startswith("```"):
            in_code_fence = not in_code_fence
            if current is not None:
                if current_block:
                    current_block.append(line)
                elif sections[current]:
                    sections[current][-1] += "\n" + line
            continue

        if in_code_fence:
            if current is not None:
                if current_block:
                    current_block.append(line)
                elif sections[current]:
                    sections[current][-1] += "\n" + line
            continue

        if stripped.startswith("## "):
            # Flush any pending complex block
            if current_block and current:
                sections[current].append("\n".join(current_block))
                current_block = []
            section_name = stripped[3:].strip()
            current = classify_section(section_name)
            continue

        if current is None:
            continue

        if stripped.startswith("### "):
            # Flush previous block
            if current_block:
                sections[current].append("\n".join(current_block))
            current_block = [line]
        elif current_block:
            if stripped == "":
                sections[current].append("\n".join(current_block))
                current_block = []
            else:
                current_block.append(line)
        elif stripped.startswith("- "):
            sections[current].append(stripped)
        elif stripped and not stripped.startswith("#"):
            # Continuation line (indented sub-items)
            if sections[current]:
                sections[current][-1] += "\n" + line

    # Flush remaining block
    if current_block and current:
        sections[current].append("\n".join(current_block))

    return sections


def insert_mission(content: str, entry: str, *, urgent: bool = False) -> str:
    """Insert a mission entry into the pending section of missions.md content.

    By default, inserts at the bottom of the pending section (FIFO queue).
    When urgent=True, inserts at the top (next to be picked up).

    Automatically stamps the entry with a queued timestamp.

    Returns the updated content string.
    """
    if not content:
        content = DEFAULT_SKELETON

    # Sanitize newlines in the entry to keep it on one line
    entry = re.sub(r"\r\n|\r|\n", " ", entry)

    # Add queued timestamp if not already present
    if _QUEUED_MARKER not in entry:
        entry = stamp_queued(entry)

    if urgent:
        # Insert at top of pending section (right after the header)
        marker = None
        for candidate in ("## Pending", "## En attente"):
            if candidate in content:
                marker = candidate
                break

        if marker:
            idx = content.index(marker) + len(marker)
            while idx < len(content) and content[idx] == "\n":
                idx += 1
            content = content[:idx] + f"\n{entry}\n" + content[idx:]
        else:
            content += f"\n## Pending\n\n{entry}\n"
    else:
        # Insert at bottom of pending section (before next ## header)
        lines = content.splitlines()
        in_pending = False
        last_content_line = None
        pending_header_line = None

        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.lower() in ("## pending", "## en attente"):
                in_pending = True
                pending_header_line = i
                continue
            if in_pending and stripped.startswith("## "):
                break  # Next section
            if in_pending and (stripped.startswith("- ") or
                               (stripped and not stripped.startswith("#") and
                                last_content_line is not None)):
                last_content_line = i

        if pending_header_line is not None:
            insert_after = last_content_line if last_content_line is not None else pending_header_line
            lines.insert(insert_after + 1, entry)
            content = "\n".join(lines)
        else:
            content += f"\n## Pending\n\n{entry}\n"

    return normalize_content(content)


def count_pending(content: str) -> int:
    """Count pending mission items."""
    sections = parse_sections(content)
    return len(sections["pending"])


def extract_next_pending(content: str, project_name: str = "") -> str:
    """Return the first pending mission block, or empty string if none.

    A mission block starts with ``- ...`` and includes any continuation lines
    that follow (indented lines, code-fence blocks, or other non-item content).

    If project_name is given, only returns missions tagged [projet:name]
    or [project:name], or under a ### project:name / ### projet:name sub-header,
    or untagged missions (outside any sub-header).
    """
    in_pending = False
    current_subheader_project = ""  # project from ### sub-header, empty = no sub-header
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        stripped_lower = stripped.lower()

        if stripped_lower.startswith("## "):
            section_key = classify_section(stripped_lower[3:].strip())
            if section_key == "pending":
                in_pending = True
                current_subheader_project = ""
            elif in_pending:
                break  # Left the pending section
            i += 1
            continue

        if not in_pending:
            i += 1
            continue

        # Track ### project:X sub-headers within pending section
        if stripped_lower.startswith("### "):
            subheader_match = re.search(
                r"###\s+projec?t\s*:\s*([a-zA-Z0-9_-]+)", stripped, re.IGNORECASE
            )
            if subheader_match:
                current_subheader_project = subheader_match.group(1).lower()
            else:
                current_subheader_project = ""
            i += 1
            continue

        if not stripped.startswith("- "):
            i += 1
            continue

        # Skip strikethrough (completed) items still lingering in Pending
        if re.match(r"^- ~~.+~~", stripped):
            i += 1
            continue

        if project_name:
            # 1. Check inline tag first (takes priority)
            tag_match = re.search(r"\[projec?t:([a-zA-Z0-9_-]+)\]", line)
            if tag_match:
                if tag_match.group(1).lower() != project_name.lower():
                    i += 1
                    continue
            elif current_subheader_project:
                # 2. Check sub-header project context
                if current_subheader_project != project_name.lower():
                    i += 1
                    continue
            # 3. No tag and no sub-header = untagged, always matches

        # Found a matching mission — collect continuation lines
        block_lines = [stripped]
        i += 1
        in_code_fence = False
        while i < len(lines):
            cont_line = lines[i]
            cont_stripped = cont_line.strip()

            # Track code fences
            if cont_stripped.startswith("```"):
                in_code_fence = not in_code_fence
                block_lines.append(cont_line)
                i += 1
                continue

            # Inside code fence — always include
            if in_code_fence:
                block_lines.append(cont_line)
                i += 1
                continue

            # New item, section header, or sub-header — stop
            if (cont_stripped.startswith("- ") or
                    cont_stripped.startswith("## ") or
                    cont_stripped.startswith("### ")):
                break

            # Empty line — stop (unless inside code fence, handled above)
            if cont_stripped == "":
                break

            # Continuation line (indented or other non-item content)
            block_lines.append(cont_line)
            i += 1

        return "\n".join(block_lines)

    return ""


def extract_tdd_tag(line: str) -> bool:
    """Check if a mission line or block contains the [tdd] tag.

    Returns True if [tdd] is found (case-insensitive), False otherwise.
    """
    return bool(re.search(r'\[tdd\]', line, re.IGNORECASE))


def extract_project_tag(line: str) -> str:
    """Extract project name from a mission line or block, or 'default'.

    Checks for:
    1. Inline tag: [project:name] or [projet:name]
    2. Sub-header: ### project:name or ### projet:name
    """
    # Inline tag (brackets)
    match = re.search(r'\[(?:project|projet):([a-zA-Z0-9_-]+)\]', line)
    if match:
        return match.group(1)
    # Sub-header format (### project:name)
    match = re.search(r'###\s+projec?t\s*:\s*([a-zA-Z0-9_-]+)', line, re.IGNORECASE)
    if match:
        return match.group(1)
    return "default"


def group_by_project(content: str) -> Dict[str, Dict[str, List[str]]]:
    """Parse missions and group them by project.

    Returns {project: {"pending": [...], "in_progress": [...]}}.
    """
    result = defaultdict(lambda: {"pending": [], "in_progress": []})

    sections = parse_sections(content)
    for item in sections["pending"]:
        project = extract_project_tag(item)
        result[project]["pending"].append(item)
    for item in sections["in_progress"]:
        project = extract_project_tag(item)
        result[project]["in_progress"].append(item)

    return dict(result)


def normalize_content(content: str) -> str:
    """Normalize missions.md content by collapsing excessive blank lines.

    Rules:
    - Max 1 blank line between any two non-blank lines
    - No trailing blank lines at end of file (just a final newline)
    - Preserves all non-blank content exactly as-is
    """
    lines = content.splitlines()
    result = []
    prev_blank = False

    for line in lines:
        is_blank = line.strip() == ""
        if is_blank and prev_blank:
            continue  # skip consecutive blank lines
        result.append(line)
        prev_blank = is_blank

    # Strip trailing blank lines, ensure single final newline
    while result and result[-1].strip() == "":
        result.pop()

    return "\n".join(result) + "\n" if result else ""


def parse_ideas(content: str) -> List[str]:
    """Parse the ## Ideas section and return a list of idea items.

    Items start with "- ..." and may include continuation lines (indented
    or non-heading text that follows). The Ideas section is intentionally
    not part of _SECTION_MAP — ideas are never picked up by the agent loop.
    """
    ideas = []
    in_ideas = False

    for line in content.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("## "):
            section_name = stripped[3:].strip().lower()
            if section_name == "ideas":
                in_ideas = True
            elif in_ideas:
                break  # Left the Ideas section
            continue

        if not in_ideas:
            continue

        if stripped.startswith("- "):
            ideas.append(stripped)
        elif stripped and not stripped.startswith("#") and ideas:
            # Continuation line — append to last idea
            ideas[-1] += "\n" + line

    return ideas


def insert_idea(content: str, entry: str) -> str:
    """Insert an idea entry at the bottom of the Ideas section of missions.md.

    Creates the section if it doesn't exist (right after # Missions header).
    Returns the updated content string.
    """
    if not content:
        content = DEFAULT_SKELETON

    # Find ## Ideas section and insert at the bottom
    lines = content.splitlines()
    in_ideas = False
    last_idea_line = None
    ideas_header_line = None

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.lower() == "## ideas":
            in_ideas = True
            ideas_header_line = i
            continue
        if in_ideas and stripped.startswith("## "):
            break  # Next section
        if in_ideas and (stripped.startswith("- ") or
                         (stripped and not stripped.startswith("#") and last_idea_line is not None)):
            last_idea_line = i

    if ideas_header_line is not None:
        # Insert after the last idea, or after the header if section is empty
        insert_after = last_idea_line if last_idea_line is not None else ideas_header_line
        lines.insert(insert_after + 1, entry)
        return normalize_content("\n".join(lines))

    # No Ideas section — create one after # Missions
    if "# Missions" in content:
        idx = content.index("# Missions") + len("# Missions")
        while idx < len(content) and content[idx] == "\n":
            idx += 1
        content = content[:idx] + f"\n## Ideas\n\n{entry}\n\n" + content[idx:]
    else:
        content = f"# Missions\n\n## Ideas\n\n{entry}\n\n" + content

    return normalize_content(content)


def delete_idea(content: str, index: int) -> Tuple[str, Optional[str]]:
    """Delete an idea by 1-based index from the Ideas section.

    Handles multi-line ideas (continuation lines after the initial "- ..." line).
    Returns (updated_content, deleted_text) or (original_content, None) if
    the index is out of range.
    """
    ideas = parse_ideas(content)
    if index < 1 or index > len(ideas):
        return content, None

    target = ideas[index - 1]

    # Find and remove all lines belonging to this idea
    lines = content.splitlines()
    idea_count = 0
    in_ideas = False
    remove_start = None

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.lower().startswith("## "):
            section_name = stripped[3:].strip().lower()
            if section_name == "ideas":
                in_ideas = True
            elif in_ideas:
                break
            continue

        if in_ideas and stripped.startswith("- "):
            # If we were collecting lines for a previous match, stop
            if remove_start is not None:
                break
            idea_count += 1
            if idea_count == index:
                remove_start = i
        elif in_ideas and remove_start is not None:
            if stripped and not stripped.startswith("#"):
                # Continuation line — include in removal
                continue
            else:
                # Empty line or heading — stop collecting
                break

    if remove_start is not None:
        # Determine how many lines to remove
        remove_end = remove_start + 1
        for j in range(remove_start + 1, len(lines)):
            stripped = lines[j].strip()
            if stripped.startswith("- ") or stripped.startswith("## "):
                break
            if stripped == "":
                break
            remove_end = j + 1
        del lines[remove_start:remove_end]
        return normalize_content("\n".join(lines)), target

    return content, None


def promote_idea(content: str, index: int) -> Tuple[str, Optional[str]]:
    """Promote an idea (by 1-based index) to the Pending section.

    Removes the idea from Ideas, adds it to Pending.
    Returns (updated_content, promoted_text) or (original_content, None).
    """
    updated, deleted = delete_idea(content, index)
    if deleted is None:
        return content, None

    # Insert the deleted idea into the pending section (at the top — promoted ideas are urgent)
    updated = insert_mission(updated, deleted, urgent=True)
    return updated, deleted


def promote_all_ideas(content: str) -> Tuple[str, List[str]]:
    """Promote all ideas to the Pending section.

    Returns (updated_content, list_of_promoted_texts).
    If no ideas exist, returns (original_content, []).
    """
    ideas = parse_ideas(content)
    if not ideas:
        return content, []

    # Promote from last to first so indices stay valid
    promoted = []
    updated = content
    for i in range(len(ideas), 0, -1):
        updated, text = promote_idea(updated, i)
        if text is not None:
            promoted.append(text)

    promoted.reverse()  # restore original order
    return updated, promoted


def list_pending(content: str) -> List[str]:
    """Return all pending mission lines.

    Filters out strikethrough (completed) items that may linger in Pending.
    """
    sections = parse_sections(content)
    return [
        item for item in sections["pending"]
        if not re.match(r"^- ~~.+~~", item.strip())
    ]


def _find_item_extent(lines: List[str], item_start: int, section_end: int) -> int:
    """Return the exclusive end index for a ``- `` item and its continuations."""
    end = item_start + 1
    for j in range(item_start + 1, section_end):
        stripped = lines[j].strip()
        if stripped == "" or stripped.startswith("- ") or stripped.startswith("#"):
            break
        end = j + 1
    return end


def _splice_pending_item(
    lines: List[str], remove_start: int, remove_end: int,
) -> Tuple[str, str]:
    """Remove lines[remove_start:remove_end] and return (content, removed)."""
    removed_text = "\n".join(lines[remove_start:remove_end])
    new_lines = lines[:remove_start] + lines[remove_end:]
    return normalize_content("\n".join(new_lines)), removed_text


def _remove_pending_by_index(
    content: str, target_idx: int,
) -> Optional[Tuple[str, str]]:
    """Remove the Nth pending item (0-indexed) from missions.md content.

    Returns (updated_content, removed_text) or None if the item cannot
    be found in the raw content.
    """
    lines = content.splitlines()
    boundaries = find_section_boundaries(lines)
    if "pending" not in boundaries:
        return None

    start, end = boundaries["pending"]
    pending_count = 0

    for i in range(start + 1, end):
        if lines[i].strip().startswith("- "):
            if pending_count == target_idx:
                return _splice_pending_item(lines, i, _find_item_extent(lines, i, end))
            pending_count += 1

    return None


def cancel_pending_mission(content: str, identifier: str) -> Tuple[str, str]:
    """Cancel a pending mission by number (1-indexed) or keyword match.

    Args:
        content: Full missions.md content.
        identifier: A number string ("3") or keyword ("fix auth").

    Returns:
        (updated_content, cancelled_mission_text)

    Raises:
        ValueError: If no matching mission is found.
    """
    pending = list_pending(content)
    if not pending:
        raise ValueError("No pending missions.")

    identifier = identifier.strip()

    # Determine which pending item to cancel
    target_idx = None
    if identifier.isdigit():
        num = int(identifier) - 1
        if num < 0 or num >= len(pending):
            raise ValueError(
                f"Mission #{identifier} not found. "
                f"There are {len(pending)} pending mission(s)."
            )
        target_idx = num
    else:
        # Keyword match (case-insensitive, first match)
        keyword = identifier.lower()
        for i, item in enumerate(pending):
            if keyword in item.lower():
                target_idx = i
                break
        if target_idx is None:
            raise ValueError(f'No pending mission matching "{identifier}".')

    target_text = pending[target_idx]

    result = _remove_pending_by_index(content, target_idx)
    if result is None:
        raise ValueError("Could not locate mission in file content.")

    return result[0], target_text


def _remove_pending_by_text(
    content: str, needle: str,
) -> Optional[Tuple[str, str]]:
    """Remove the first pending ``- `` item containing *needle*.

    Scans raw lines in the Pending section instead of going through
    ``parse_sections`` indexes, so ``### project:X`` sub-headers
    don't cause mismatches.

    Returns ``(updated_content, removed_text)`` or ``None`` when no match.
    """
    return _remove_item_by_text(content, needle, "pending")


def _remove_item_by_text(
    content: str, needle: str, section_key: str,
) -> Optional[Tuple[str, str]]:
    """Remove the first ``- `` item containing *needle* from the given section.

    Returns ``(updated_content, removed_text)`` or ``None`` when no match.
    """
    lines = content.splitlines()
    boundaries = find_section_boundaries(lines)
    if section_key not in boundaries:
        return None

    start, end = boundaries[section_key]

    for i in range(start + 1, end):
        stripped = lines[i].strip()
        if stripped.startswith("- ") and needle in stripped:
            return _splice_pending_item(lines, i, _find_item_extent(lines, i, end))

    return None


def _move_pending_to_section(
    content: str, mission_text: str, section_key: str, marker: str, header: str,
) -> str:
    """Move a mission from Pending (or In Progress) to a target section.

    Shared implementation for complete_mission() and fail_mission().
    Searches Pending first, then falls back to In Progress.
    Returns content unchanged if the mission is not found in either section.
    """
    needle = mission_text.strip()
    result = _remove_pending_by_text(content, needle)
    if result is None:
        result = _remove_item_by_text(content, needle, "in_progress")
    if result is None:
        return content

    updated = result[0]

    # Use original line text (preserves [project:X] tags) instead of needle.
    removed = result[1].strip()
    display = removed.removeprefix("- ") if removed.startswith("- ") else removed

    timestamp = time.strftime("%Y-%m-%d %H:%M")
    entry = f"- {display} {marker} ({timestamp})"

    lines = updated.splitlines()
    boundaries = find_section_boundaries(lines)
    if section_key in boundaries:
        start, end = boundaries[section_key]
        insert_at = start + 1
        while insert_at < end and lines[insert_at].strip() == "":
            insert_at += 1
        lines.insert(insert_at, entry)
        return normalize_content("\n".join(lines))

    return normalize_content(updated + f"\n## {header}\n\n{entry}\n")


def _flush_in_progress_to_done(content: str) -> str:
    """Move all In Progress missions to Done.

    Sanity enforcement: only one mission should be in progress at a time.
    When a new mission is about to start, any stale In Progress missions
    are automatically completed with a timestamp.
    """
    sections = parse_sections(content)
    stale = sections.get("in_progress", [])
    if not stale:
        return content

    for item in stale:
        # Extract the first line for the needle
        first_line = item.split("\n")[0].strip()
        if first_line.startswith("- "):
            first_line = first_line[2:]
        content = _move_in_progress_to_done(content, first_line)

    return content


def _move_in_progress_to_done(content: str, needle: str) -> str:
    """Move a single mission from In Progress to Done with a timestamp."""
    result = _remove_item_by_text(content, needle, "in_progress")
    if result is None:
        return content

    updated = result[0]
    removed = result[1].strip()
    display = removed.removeprefix("- ") if removed.startswith("- ") else removed

    timestamp = time.strftime("%Y-%m-%d %H:%M")
    entry = f"- {display} \u2705 ({timestamp})"

    lines = updated.splitlines()
    boundaries = find_section_boundaries(lines)
    if "done" in boundaries:
        start, end = boundaries["done"]
        insert_at = start + 1
        while insert_at < end and lines[insert_at].strip() == "":
            insert_at += 1
        lines.insert(insert_at, entry)
        return normalize_content("\n".join(lines))

    return normalize_content(updated + f"\n## Done\n\n{entry}\n")


def start_mission(content: str, mission_text: str) -> str:
    """Move a mission from Pending to In Progress with a started timestamp.

    Used at the beginning of mission execution to mark it as active.
    As a sanity enforcement, any existing In Progress missions are moved
    to Done before the new mission is inserted — only one mission can be
    in progress at a time.
    Returns content unchanged if the mission is not found in Pending.
    """
    needle = mission_text.strip()
    result = _remove_pending_by_text(content, needle)
    if result is None:
        return content

    from app.security_audit import MISSION_START, log_event
    log_event(MISSION_START, details={
        "mission": mission_text,
        "project": extract_project_tag(mission_text),
    })

    updated = result[0]
    removed = result[1].strip()
    # Keep the original line text (with project tag, queued timestamp etc)
    entry = removed if removed.startswith("- ") else f"- {removed}"

    # Add started timestamp
    entry = stamp_started(entry)

    # Sanity enforcement: move any existing In Progress missions to Done
    updated = _flush_in_progress_to_done(updated)

    lines = updated.splitlines()
    boundaries = find_section_boundaries(lines)
    if "in_progress" in boundaries:
        start, end = boundaries["in_progress"]
        insert_at = start + 1
        while insert_at < end and lines[insert_at].strip() == "":
            insert_at += 1
        lines.insert(insert_at, entry)
        return normalize_content("\n".join(lines))

    return normalize_content(updated + f"\n## In Progress\n\n{entry}\n")


def complete_mission(content: str, mission_text: str) -> str:
    """Move a mission from Pending (or In Progress) to Done with a timestamp.

    Searches Pending first, then In Progress.

    Returns:
        Updated content string. Returns original content unchanged if
        the mission is not found in either section.
    """
    from app.security_audit import MISSION_COMPLETE, log_event
    log_event(MISSION_COMPLETE, details={"mission": mission_text})
    return _move_pending_to_section(content, mission_text, "done", "\u2705", "Done")


def fail_mission(content: str, mission_text: str) -> str:
    """Move a mission from Pending (or In Progress) to Failed with a timestamp.

    Same pattern as complete_mission() but moves to ## Failed instead of ## Done.
    Searches Pending first, then In Progress.

    Returns content unchanged if the mission is not found in either section.
    """
    from app.security_audit import MISSION_FAIL, log_event
    log_event(MISSION_FAIL, details={"mission": mission_text})
    return _move_pending_to_section(content, mission_text, "failed", "\u274c", "Failed")


def requeue_mission(content: str, mission_text: str) -> str:
    """Move a mission from In Progress back to Pending.

    Used when an error is recoverable by human intervention (e.g. re-login)
    rather than a mission failure.  Strips the started timestamp so the
    mission looks like a fresh pending item.

    Returns content unchanged if the mission is not found in In Progress.
    """
    needle = mission_text.strip()
    result = _remove_item_by_text(content, needle, "in_progress")
    if result is None:
        return content

    updated, removed = result
    # Strip the "- " prefix and started marker so we re-insert cleanly
    display = removed.strip()
    if display.startswith("- "):
        display = display[2:]
    # Remove started timestamp (▶(2026-03-26T22:00))
    display = _STARTED_PATTERN.sub("", display).strip()

    entry = f"- {display}"

    lines = updated.splitlines()
    boundaries = find_section_boundaries(lines)
    if "pending" in boundaries:
        start, end = boundaries["pending"]
        insert_at = start + 1
        # Skip blank lines after header
        while insert_at < end and lines[insert_at].strip() == "":
            insert_at += 1
        lines.insert(insert_at, entry)
        return normalize_content("\n".join(lines))

    # No Pending section — create one
    return normalize_content(updated + f"\n## Pending\n\n{entry}\n")


def clean_mission_display(text: str, max_length: int = 120) -> str:
    """Clean a mission or idea line for display.

    For multi-line ideas, only the first line is shown. Strips leading "- ",
    converts [project:X] tags to readable [X] prefix, and truncates long lines.
    """
    # For multi-line ideas, use only the first line
    if "\n" in text:
        text = text.split("\n")[0]

    # Strip leading "- "
    if text.startswith("- "):
        text = text[2:]

    # Strip project tag but keep project name as prefix
    tag_match = re.search(r'\[projec?t:([a-zA-Z0-9_-]+)\]\s*', text)
    if tag_match:
        project = tag_match.group(1)
        text = re.sub(r'\[projec?t:[a-zA-Z0-9_-]+\]\s*', '', text)
        text = f"[{project}] {text}"

    # Strip trailing GitHub origin marker (displayed by /list as a leading hint)
    text = text.rstrip()
    if text.endswith("📬"):
        text = text[:-1].rstrip()

    # Truncate for readability
    if len(text) > max_length:
        text = text[:max_length - 3] + "..."

    return text


def find_section_boundaries(lines: List[str]) -> Dict[str, Tuple[int, int]]:
    """Find line indices for each section.

    Returns {section_key: (start_line, end_line)} where start_line is the
    ## header line and end_line is the next ## header (or len(lines)).
    """
    boundaries = {}
    section_order = []

    for i, line in enumerate(lines):
        stripped = line.strip().lower()
        if stripped.startswith("## "):
            key = classify_section(stripped[3:].strip())
            if key:
                section_order.append((key, i))

    for idx, (key, start) in enumerate(section_order):
        if idx + 1 < len(section_order):
            end = section_order[idx + 1][1]
        else:
            end = len(lines)
        boundaries[key] = (start, end)

    return boundaries


def reorder_mission(content: str, position: int, target: int = 1) -> Tuple[str, str]:
    """Move a pending mission from one position to another.

    Args:
        content: Full missions.md content.
        position: 1-indexed position of the mission to move (in pending list).
        target: 1-indexed target position (default 1 = top of queue).

    Returns:
        (new_content, moved_display_text) tuple.

    Raises:
        ValueError: If position or target is invalid, or no pending missions.
    """
    lines = content.splitlines()
    boundaries = find_section_boundaries(lines)

    if "pending" not in boundaries:
        raise ValueError("No pending section found.")

    start, end = boundaries["pending"]

    # Collect pending items as (start_line_idx, end_line_idx) tuples
    items = []
    i = start + 1  # Skip the ## header line
    while i < end:
        stripped = lines[i].strip()
        if stripped.startswith("- "):
            item_start = i
            i += 1
            # Include continuation lines (indented, not a new item or header)
            while i < end:
                next_stripped = lines[i].strip()
                if (next_stripped.startswith("- ") or
                        next_stripped.startswith("## ") or
                        next_stripped.startswith("### ") or
                        next_stripped == ""):
                    break
                i += 1
            items.append((item_start, i))
        else:
            i += 1

    if not items:
        raise ValueError("No pending missions to reorder.")

    if position < 1 or position > len(items):
        raise ValueError(
            f"Invalid position: {position}. Queue has {len(items)} pending mission(s)."
        )

    if target < 1 or target > len(items):
        raise ValueError(
            f"Invalid target: {target}. Queue has {len(items)} pending mission(s)."
        )

    if position == target:
        raise ValueError(f"Mission #{position} is already at position {target}.")

    # Extract the item to move
    moved_start, moved_end = items[position - 1]
    moved_lines = lines[moved_start:moved_end]
    moved_text = "\n".join(moved_lines)

    # Remove the moved item's lines
    new_lines = lines[:moved_start] + lines[moved_end:]

    # Recalculate item positions after removal
    new_boundaries = find_section_boundaries(new_lines)
    new_start, new_end = new_boundaries["pending"]

    new_items = []
    j = new_start + 1
    while j < new_end:
        s = new_lines[j].strip()
        if s.startswith("- "):
            item_start_j = j
            j += 1
            while j < new_end:
                ns = new_lines[j].strip()
                if (ns.startswith("- ") or
                        ns.startswith("## ") or
                        ns.startswith("### ") or
                        ns == ""):
                    break
                j += 1
            new_items.append(item_start_j)
        else:
            j += 1

    # Determine insertion line index
    if target == 1:
        insert_idx = new_start + 1
        while insert_idx < new_end and new_lines[insert_idx].strip() == "":
            insert_idx += 1
    elif target - 1 < len(new_items):
        insert_idx = new_items[target - 1]
    else:
        if new_items:
            last_start = new_items[-1]
            insert_idx = last_start + 1
            while insert_idx < new_end:
                ns = new_lines[insert_idx].strip()
                if (ns.startswith("- ") or
                        ns.startswith("## ") or
                        ns.startswith("### ") or
                        ns == ""):
                    break
                insert_idx += 1
        else:
            insert_idx = new_start + 1

    # Insert the moved lines
    result_lines = new_lines[:insert_idx] + moved_lines + new_lines[insert_idx:]

    display = clean_mission_display(moved_text)
    return normalize_content("\n".join(result_lines)), display


def edit_pending_mission(content: str, position: int, new_text: str) -> Tuple[str, str]:
    """Replace the text of a pending mission at the given 1-indexed position.

    Args:
        content: Full missions.md content.
        position: 1-indexed position in the pending list.
        new_text: New mission text (without leading "- ").

    Returns:
        (updated_content, new_display_text) tuple.

    Raises:
        ValueError: If position is invalid or new_text is empty.
    """
    new_text = new_text.strip()
    # Strip leading "- " if the user accidentally includes it
    if new_text.startswith("- "):
        new_text = new_text[2:].strip()
    if not new_text:
        raise ValueError("Mission text cannot be empty.")

    lines = content.splitlines()
    boundaries = find_section_boundaries(lines)

    if "pending" not in boundaries:
        raise ValueError("No pending section found.")

    start, end = boundaries["pending"]

    # Collect pending items as (start_line_idx, end_line_idx) tuples
    items = []
    i = start + 1
    while i < end:
        stripped = lines[i].strip()
        if stripped.startswith("- "):
            item_start = i
            i += 1
            while i < end:
                next_stripped = lines[i].strip()
                if (next_stripped.startswith("- ") or
                        next_stripped.startswith("## ") or
                        next_stripped.startswith("### ") or
                        next_stripped == ""):
                    break
                i += 1
            items.append((item_start, i))
        else:
            i += 1

    if not items:
        raise ValueError("No pending missions to edit.")

    if position < 1 or position > len(items):
        raise ValueError(
            f"Invalid position: {position}. Queue has {len(items)} pending mission(s)."
        )

    item_start, item_end = items[position - 1]
    old_line = lines[item_start].strip()

    # Preserve existing timestamps from the old line
    old_timestamps = ""
    for pattern in [_QUEUED_PATTERN, _STARTED_PATTERN]:
        match = pattern.search(old_line)
        if match:
            old_timestamps += " " + match.group(0).strip()

    # Build the new line
    new_line = f"- {new_text}{old_timestamps}"

    # Replace the item (first line only; drop continuation lines)
    new_lines = lines[:item_start] + [new_line] + lines[item_end:]

    display = clean_mission_display(new_line)
    return normalize_content("\n".join(new_lines)), display


def prune_done_section(content: str, keep: int = 50) -> Tuple[str, int]:
    """Prune the Done section to keep only the most recent items.

    Old done items are removed entirely — they serve no operational purpose
    and inflate file size (missions.md can grow to 200KB+ without pruning).

    Args:
        content: Full missions.md content.
        keep: Number of most recent Done items to keep.

    Returns:
        (new_content, pruned_count) tuple.
    """
    lines = content.splitlines()
    boundaries = find_section_boundaries(lines)

    if "done" not in boundaries:
        return content, 0

    start, end = boundaries["done"]

    # Collect done items as line ranges
    items = []  # list of (item_start, item_end) tuples
    i = start + 1  # skip ## header
    while i < end:
        stripped = lines[i].strip()
        if stripped.startswith("- "):
            item_start = i
            i += 1
            while i < end:
                next_stripped = lines[i].strip()
                if (next_stripped.startswith("- ") or
                        next_stripped.startswith("## ") or
                        next_stripped.startswith("### ") or
                        next_stripped == ""):
                    break
                i += 1
            items.append((item_start, i))
        else:
            i += 1

    if len(items) <= keep:
        return content, 0

    pruned_count = len(items) - keep
    # Keep the last `keep` items (most recent are at the top of Done)
    keep_items = items[:keep]

    # Build the set of lines to keep from the Done section
    keep_lines = set()
    for item_start, item_end in keep_items:
        for j in range(item_start, item_end):
            keep_lines.add(j)

    # Rebuild: header + kept items + everything after Done section
    new_lines = lines[:start + 1]  # everything before and including ## Done
    for j in range(start + 1, end):
        if j in keep_lines or lines[j].strip() == "":
            new_lines.append(lines[j])
            # Only keep the first blank line after a removed block
    new_lines.extend(lines[end:])  # everything after Done section

    return normalize_content("\n".join(new_lines)), pruned_count


# ---------------------------------------------------------------------------
# Parallel session support
# ---------------------------------------------------------------------------

# Session tag pattern: [session:abc123] embedded in in-progress mission lines
_SESSION_TAG_PATTERN = re.compile(r'\[session:([a-zA-Z0-9_-]+)\]')


def _stamp_session(entry: str, session_id: str) -> str:
    """Add a session tag to a mission entry."""
    return f"[session:{session_id}] {entry}" if session_id else entry


def _extract_session_id(text: str) -> str:
    """Extract session ID from a mission line, or empty string."""
    match = _SESSION_TAG_PATTERN.search(text)
    return match.group(1) if match else ""


def _strip_session_tag(text: str) -> str:
    """Remove [session:X] tag from a mission line."""
    return _SESSION_TAG_PATTERN.sub("", text).strip()


def pick_missions(
    content: str,
    n: int = 1,
    exclude_projects: Optional[List[str]] = None,
) -> List[str]:
    """Extract up to N pending missions, preferring project diversity.

    When multiple missions are available, prefers picking from different
    projects before taking multiple missions from the same project.
    This maximizes parallelism across the project portfolio.

    Args:
        content: missions.md content.
        n: Maximum number of missions to pick.
        exclude_projects: Project names to skip (e.g., already have active sessions).

    Returns:
        List of mission text strings (without leading "- ").
    """
    if n <= 0:
        return []

    if exclude_projects is None:
        exclude_projects = []
    exclude_set = {p.lower() for p in exclude_projects}

    sections = parse_sections(content)
    pending = sections.get("pending", [])
    if not pending:
        return []

    # Build (project, mission_text) pairs
    candidates: List[Tuple[str, str]] = []
    for item in pending:
        first_line = item.split("\n")[0].strip()
        if first_line.startswith("- "):
            first_line = first_line[2:]
        if re.match(r"^~~.+~~", first_line):
            continue  # Skip strikethrough
        project = extract_project_tag(item)
        if project.lower() in exclude_set:
            continue
        candidates.append((project, first_line))

    if not candidates:
        return []

    # Pick with project diversity: round-robin across projects
    picked: List[str] = []
    picked_projects: Dict[str, int] = defaultdict(int)
    remaining = list(candidates)

    while len(picked) < n and remaining:
        # Find the project with the fewest picks so far
        best_idx = 0
        best_count = picked_projects.get(remaining[0][0], 0)
        for idx, (proj, _) in enumerate(remaining):
            count = picked_projects.get(proj, 0)
            if count < best_count:
                best_count = count
                best_idx = idx

        proj, text = remaining.pop(best_idx)
        picked.append(text)
        picked_projects[proj] += 1

    return picked


def start_mission_parallel(
    content: str,
    mission_text: str,
    session_id: str,
) -> str:
    """Move a mission from Pending to In Progress for parallel execution.

    Unlike start_mission(), this does NOT flush existing In Progress
    missions — multiple missions can be in progress simultaneously.
    Each in-progress entry is tagged with [session:ID] for tracking.

    Args:
        content: missions.md content.
        mission_text: The mission text to start.
        session_id: Unique session identifier for tracking.

    Returns:
        Updated content string. Unchanged if mission not found in Pending.
    """
    needle = mission_text.strip()
    result = _remove_pending_by_text(content, needle)
    if result is None:
        return content

    from app.security_audit import MISSION_START, log_event
    log_event(MISSION_START, details={
        "mission": mission_text,
        "project": extract_project_tag(mission_text),
        "session_id": session_id,
    })

    updated = result[0]
    removed = result[1].strip()
    entry = removed if removed.startswith("- ") else f"- {removed}"

    # Add session tag and started timestamp
    # Insert session tag after "- " prefix
    if entry.startswith("- "):
        entry = f"- [session:{session_id}] {entry[2:]}"
    entry = stamp_started(entry)

    # Do NOT flush existing In Progress — parallel mode allows multiple
    lines = updated.splitlines()
    boundaries = find_section_boundaries(lines)
    if "in_progress" in boundaries:
        start, end = boundaries["in_progress"]
        insert_at = start + 1
        while insert_at < end and lines[insert_at].strip() == "":
            insert_at += 1
        lines.insert(insert_at, entry)
        return normalize_content("\n".join(lines))

    return normalize_content(updated + f"\n## In Progress\n\n{entry}\n")


def complete_mission_by_session(content: str, session_id: str) -> str:
    """Move an in-progress mission to Done, matching by session ID.

    Used in parallel mode where multiple missions are in progress.
    Finds the mission tagged with [session:ID] and completes it.

    Returns content unchanged if no mission matches the session ID.
    """
    return _move_by_session(content, session_id, "done", "\u2705", "Done")


def fail_mission_by_session(content: str, session_id: str) -> str:
    """Move an in-progress mission to Failed, matching by session ID.

    Returns content unchanged if no mission matches the session ID.
    """
    return _move_by_session(content, session_id, "failed", "\u274c", "Failed")


def _move_by_session(
    content: str,
    session_id: str,
    target_section: str,
    marker: str,
    header: str,
) -> str:
    """Move an in-progress mission to a target section, matching by session ID."""
    tag = f"[session:{session_id}]"
    lines = content.splitlines()
    boundaries = find_section_boundaries(lines)

    if "in_progress" not in boundaries:
        return content

    start, end = boundaries["in_progress"]

    # Find the line with the matching session tag
    for i in range(start + 1, end):
        stripped = lines[i].strip()
        if stripped.startswith("- ") and tag in stripped:
            item_end = _find_item_extent(lines, i, end)
            result = _splice_pending_item(lines, i, item_end)
            if result is None:
                return content
            updated, removed = result
            removed = removed.strip()
            # Strip session tag and "- " prefix for display
            display = removed.removeprefix("- ") if removed.startswith("- ") else removed
            display = _SESSION_TAG_PATTERN.sub("", display).strip()

            timestamp = time.strftime("%Y-%m-%d %H:%M")
            entry = f"- {display} {marker} ({timestamp})"

            lines2 = updated.splitlines()
            boundaries2 = find_section_boundaries(lines2)
            if target_section in boundaries2:
                s2, e2 = boundaries2[target_section]
                insert_at = s2 + 1
                while insert_at < e2 and lines2[insert_at].strip() == "":
                    insert_at += 1
                lines2.insert(insert_at, entry)
                return normalize_content("\n".join(lines2))

            return normalize_content(updated + f"\n## {header}\n\n{entry}\n")

    return content


def count_in_progress(content: str) -> int:
    """Count the number of missions currently in progress."""
    sections = parse_sections(content)
    return len(sections.get("in_progress", []))


# ---------------------------------------------------------------------------
# Quarantine helpers
# ---------------------------------------------------------------------------

# Max quarantine file size in bytes. Once exceeded, the oldest half of
# entries is pruned to make room.  100 KB is ~200 entries at ~500 bytes each.
QUARANTINE_MAX_BYTES = 100_000


def quarantine_mission(
    quarantine_path: "Path",
    text: str,
    reason: str,
    source: str = "unknown",
) -> bool:
    """Append a flagged mission to the quarantine file.

    Args:
        quarantine_path: Path to missions-quarantine.md.
        text: The mission text (truncated to 500 chars).
        reason: Why it was quarantined.
        source: Origin label (e.g. "telegram", "github/@user").

    Returns:
        True if the entry was written, False on error.
    """
    from pathlib import Path  # local to avoid top-level import

    quarantine_path = Path(quarantine_path)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"- \U0001f6e1\ufe0f [{timestamp}] ({source}) {reason}: {text[:500]}\n"
    try:
        _enforce_quarantine_cap(quarantine_path)
        with open(quarantine_path, "a") as f:
            f.write(entry)
        return True
    except OSError:
        return False


def _enforce_quarantine_cap(path: "Path") -> None:
    """If the quarantine file exceeds QUARANTINE_MAX_BYTES, prune oldest half."""
    from pathlib import Path

    path = Path(path)
    if not path.exists():
        return
    size = path.stat().st_size
    if size <= QUARANTINE_MAX_BYTES:
        return
    lines = path.read_text().splitlines(keepends=True)
    # Keep the newer half
    half = len(lines) // 2
    path.write_text("".join(lines[half:]))
