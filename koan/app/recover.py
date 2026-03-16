#!/usr/bin/env python3
"""
Kōan — Crash recovery

Detects missions left in "In Progress" from a previous interrupted run.
Moves simple mission items (- lines) back to "Pending".
Skips complex multi-step missions (### headers with sub-items) — those are long-running
and should not be auto-recovered.

Usage from shell:
    python3 recover.py /path/to/instance

Returns via stdout:
    Number of recovered missions (0 if none).
    Missions file is updated in-place if recovery happens.
"""

import sys
from pathlib import Path

from app.notify import format_and_send


def check_pending_journal(instance_dir: str) -> bool:
    """Check if a pending.md exists from an interrupted run. Returns True if found.

    We do NOT delete it — the next Claude session reads it for recovery context.
    We just log its presence so the human knows recovery will happen.
    """
    pending_path = Path(instance_dir) / "journal" / "pending.md"
    try:
        content = pending_path.read_text().strip()
    except FileNotFoundError:
        return False
    if content:
        lines = content.splitlines()
        # Count progress lines (after the --- separator)
        separator_seen = False
        progress_lines = 0
        for line in lines:
            if line.strip() == "---":
                separator_seen = True
                continue
            if separator_seen and line.strip():
                progress_lines += 1
        print(f"[recover] Found pending.md with {progress_lines} progress entries — next run will resume")
        return True
    return False


def recover_missions(instance_dir: str) -> int:
    """Move stale in-progress simple missions back to pending. Returns count.

    Uses modify_missions_file() for atomic read-modify-write under exclusive lock,
    preventing race conditions with concurrent mission additions.
    """
    missions_path = Path(instance_dir) / "missions.md"
    if not missions_path.exists():
        return 0

    from app.missions import find_section_boundaries, normalize_content
    from app.utils import modify_missions_file

    recovered_count = 0

    def _recover_transform(content: str) -> str:
        nonlocal recovered_count
        lines = content.splitlines()

        boundaries = find_section_boundaries(lines)
        if "pending" not in boundaries or "in_progress" not in boundaries:
            return content

        pending_start = boundaries["pending"][0]
        in_progress_start, in_progress_end = boundaries["in_progress"]

        # Extract simple mission items from in-progress section
        # Simple = starts with "- " and is NOT a strikethrough-only line
        # Skip ### headers and their sub-items (complex long-running missions)
        recovered = []
        remaining_in_progress = []
        in_complex_mission = False

        for i in range(in_progress_start + 1, in_progress_end):
            line = lines[i]
            stripped = line.strip()

            if stripped.startswith("### "):
                in_complex_mission = True
                remaining_in_progress.append(line)
                continue

            # Blank lines end the current complex mission block.
            # A ### header starts a complex block; a blank line closes it,
            # so subsequent "- " items are treated as standalone missions.
            if stripped == "":
                if in_complex_mission:
                    in_complex_mission = False
                remaining_in_progress.append(line)
                continue

            if in_complex_mission:
                remaining_in_progress.append(line)
                continue

            if stripped.startswith("- ") and "~~" not in stripped:
                recovered.append(line)
            elif stripped == "(aucune)" or stripped == "(none)":
                remaining_in_progress.append(line)
            else:
                remaining_in_progress.append(line)

        if not recovered:
            return content

        recovered_count = len(recovered)

        # Rebuild file with recovered missions moved to pending
        new_lines = []
        for i, line in enumerate(lines):
            if pending_start < i < in_progress_start:
                if line.strip() in ("(aucune)", "(none)"):
                    continue

            if in_progress_start < i < in_progress_end:
                continue

            new_lines.append(line)

            if i == pending_start:
                new_lines.append("")
                for m in recovered:
                    new_lines.append(m)

            if i == in_progress_start:
                for m in remaining_in_progress:
                    new_lines.append(m)
                if not any(m.strip() for m in remaining_in_progress):
                    new_lines.append("")

        return normalize_content("\n".join(new_lines) + "\n")

    modify_missions_file(missions_path, _recover_transform)
    return recovered_count


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <instance_dir>", file=sys.stderr)
        sys.exit(1)

    instance_dir = sys.argv[1]
    has_pending = check_pending_journal(instance_dir)
    count = recover_missions(instance_dir)

    if count > 0 or has_pending:
        parts = []
        if count > 0:
            parts.append(f"{count} mission(s) moved back to Pending")
        if has_pending:
            parts.append("interrupted run detected (pending.md) — will resume")
        msg = "Restart — " + ", ".join(parts) + "."
        format_and_send(msg)
        print(f"[recover] {msg}")
    else:
        print("[recover] No stale missions found")

    print(count)
