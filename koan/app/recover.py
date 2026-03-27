#!/usr/bin/env python3
"""
Kōan — Crash recovery

Detects missions left in "In Progress" from a previous interrupted run.
Classifies each stale mission and takes appropriate action:

  - dead:          Standard crash — move back to Pending (increment [r:N] counter)
  - partial:       Interrupted run with pending.md context — recover with context
  - unrecoverable: Too many recovery attempts — move to Failed, notify human

Recovery attempts are tracked via an [r:N] tag embedded in the mission text.
After MAX_RECOVERY_ATTEMPTS consecutive failures, the mission is escalated to Failed
and the human is notified via Telegram.

All recovery events are logged to instance/recovery.jsonl for forensics.

Usage from shell:
    python3 recover.py /path/to/instance [--dry-run]

Returns via stdout:
    Number of recovered missions (0 if none).
    Missions file is updated in-place if recovery happens.
"""

import fcntl
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from app.notify import format_and_send


# Number of failed recovery attempts before a mission is marked unrecoverable
MAX_RECOVERY_ATTEMPTS = 3

# Regex to parse and strip the [r:N] recovery counter tag from mission text.
# Matches any content inside [r:...] (not just digits) so malformed tags
# are still caught by strip/set operations.
_RECOVERY_COUNTER_RE = re.compile(r"\s*\[r:([^\]]*)\]")


# ---------------------------------------------------------------------------
# Recovery counter helpers
# ---------------------------------------------------------------------------

def _get_recovery_attempts(mission_line: str) -> int:
    """Parse the [r:N] counter from a mission line. Returns 0 if absent or malformed."""
    m = _RECOVERY_COUNTER_RE.search(mission_line)
    if not m:
        return 0
    try:
        return int(m.group(1))
    except (ValueError, TypeError):
        return 0


def _set_recovery_attempts(mission_line: str, n: int) -> str:
    """Set the [r:N] counter in a mission line, replacing any existing one."""
    line = _RECOVERY_COUNTER_RE.sub("", mission_line).rstrip()
    return f"{line} [r:{n}]"


def _strip_recovery_counter(mission_line: str) -> str:
    """Remove the [r:N] counter from a mission line for clean display."""
    return _RECOVERY_COUNTER_RE.sub("", mission_line).rstrip()


# ---------------------------------------------------------------------------
# State classification
# ---------------------------------------------------------------------------

def classify_mission_state(mission_line: str, has_pending_journal: bool = False) -> str:
    """Classify a stale in-progress mission's recovery state.

    States:
        "unrecoverable" — Too many attempts. Escalate to Failed, notify human.
        "partial"       — Has pending.md context from an interrupted run. Recover.
        "dead"          — Standard crash, no special context. Simple recovery.

    Args:
        mission_line: The raw mission text line.
        has_pending_journal: True if a pending.md exists from an interrupted run.

    Returns:
        One of "unrecoverable", "partial", or "dead".
    """
    attempts = _get_recovery_attempts(mission_line)
    if attempts >= MAX_RECOVERY_ATTEMPTS:
        return "unrecoverable"
    if has_pending_journal:
        return "partial"
    return "dead"


# ---------------------------------------------------------------------------
# JSONL audit log
# ---------------------------------------------------------------------------

def _log_recovery_event(
    instance_dir: str,
    mission: str,
    state: str,
    action: str,
    attempts: int,
) -> None:
    """Append a recovery event to recovery.jsonl for audit trail.

    Args:
        instance_dir: Path to instance directory.
        mission: The mission text (raw line).
        state: Classified state ("dead", "partial", "unrecoverable").
        action: Action taken ("recovered", "escalated", "skipped").
        attempts: Recovery attempt count at the time of this event.
    """
    event = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "mission": _strip_recovery_counter(mission).strip(),
        "state": state,
        "action": action,
        "attempts": attempts,
    }
    log_path = Path(instance_dir) / "recovery.jsonl"
    try:
        with open(log_path, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(json.dumps(event) + "\n")
            f.flush()
            fcntl.flock(f, fcntl.LOCK_UN)
    except OSError as e:
        print(f"[recover] Warning: could not write recovery log: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Pending journal check (unchanged)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Main recovery logic
# ---------------------------------------------------------------------------

def recover_missions(instance_dir: str, dry_run: bool = False) -> int:
    """Move stale in-progress missions back to pending or escalate to failed.

    Enhanced recovery with state classification:
    - Simple stale missions (dead/partial): move back to Pending, increment [r:N]
    - Repeatedly failing missions (unrecoverable): move to Failed, notify human

    All events are logged to recovery.jsonl for forensics.

    Uses modify_missions_file() for atomic read-modify-write under exclusive lock,
    preventing race conditions with concurrent mission additions.

    Args:
        instance_dir: Path to instance directory.
        dry_run: If True, classify and log but do not modify missions.md.

    Returns:
        Number of missions moved back to Pending (excludes escalated ones).
    """
    missions_path = Path(instance_dir) / "missions.md"
    if not missions_path.exists():
        return 0

    from app.missions import find_section_boundaries, normalize_content
    from app.utils import modify_missions_file

    # Check pending.md once for the partial state detection
    # Use try/except to avoid TOCTOU race (file deleted between check and read)
    pending_path = Path(instance_dir) / "journal" / "pending.md"
    try:
        has_pending_journal = pending_path.read_text().strip() != ""
    except FileNotFoundError:
        has_pending_journal = False

    recovered_count = 0
    escalated_missions: list = []

    def _recover_transform(content: str) -> str:
        nonlocal recovered_count, escalated_missions
        lines = content.splitlines()

        boundaries = find_section_boundaries(lines)
        if "pending" not in boundaries or "in_progress" not in boundaries:
            return content

        pending_start = boundaries["pending"][0]
        in_progress_start, in_progress_end = boundaries["in_progress"]
        failed_bounds = boundaries.get("failed")

        # Classify and sort each candidate mission
        recovered = []      # missions to move to Pending
        escalated = []      # missions to move to Failed
        remaining_in_progress = []
        in_complex_mission = False

        for i in range(in_progress_start + 1, in_progress_end):
            line = lines[i]
            stripped = line.strip()

            if stripped.startswith("### "):
                in_complex_mission = True
                remaining_in_progress.append(line)
                continue

            # Blank lines end the current complex mission block
            if stripped == "":
                if in_complex_mission:
                    in_complex_mission = False
                remaining_in_progress.append(line)
                continue

            if in_complex_mission:
                remaining_in_progress.append(line)
                continue

            if stripped.startswith("- ") and "~~" not in stripped:
                # Classify this mission
                state = classify_mission_state(line, has_pending_journal=has_pending_journal)
                attempts = _get_recovery_attempts(line)

                if dry_run:
                    print(f"[recover] [dry-run] mission={stripped!r:.60} state={state} attempts={attempts}")
                    _log_recovery_event(instance_dir, line, state, "dry_run", attempts)
                    remaining_in_progress.append(line)
                    continue

                if state == "unrecoverable":
                    escalated.append(line)
                    _log_recovery_event(instance_dir, line, state, "escalated", attempts)
                else:
                    # Increment counter and move to Pending
                    updated_line = _set_recovery_attempts(line, attempts + 1)
                    recovered.append(updated_line)
                    _log_recovery_event(instance_dir, line, state, "recovered", attempts + 1)

            elif stripped == "(aucune)" or stripped == "(none)":
                remaining_in_progress.append(line)
            else:
                remaining_in_progress.append(line)

        if not recovered and not escalated:
            return content

        recovered_count = len(recovered)
        escalated_missions = escalated

        # Rebuild file: recovered → Pending, escalated → Failed, rest stays
        new_lines = []
        for i, line in enumerate(lines):
            if pending_start < i < in_progress_start:
                if line.strip() in ("(aucune)", "(none)"):
                    continue

            if in_progress_start < i < in_progress_end:
                continue

            # Skip existing failed section content — we'll rebuild it below
            if failed_bounds and failed_bounds[0] < i < failed_bounds[1]:
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

            # Restore failed section content then append escalated missions
            if failed_bounds and i == failed_bounds[0]:
                # Re-insert original failed content (minus section boundaries we'll re-emit)
                orig_failed = lines[failed_bounds[0] + 1 : failed_bounds[1]]
                for fl in orig_failed:
                    new_lines.append(fl)
                if escalated:
                    for m in escalated:
                        clean = _strip_recovery_counter(m).rstrip()
                        new_lines.append(f"- ❌ needs_input: {clean.lstrip('- ')}")
                    new_lines.append("")

        # If there's no Failed section but we have escalated missions, append one
        if escalated and not failed_bounds:
            new_lines.append("")
            new_lines.append("## Failed")
            new_lines.append("")
            for m in escalated:
                clean = _strip_recovery_counter(m).rstrip()
                new_lines.append(f"- ❌ needs_input: {clean.lstrip('- ')}")
            new_lines.append("")

        return normalize_content("\n".join(new_lines) + "\n")

    modify_missions_file(missions_path, _recover_transform)
    return recovered_count


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]

    if not args:
        print(f"Usage: {sys.argv[0]} <instance_dir> [--dry-run]", file=sys.stderr)
        sys.exit(1)

    instance_dir = args[0]
    has_pending = check_pending_journal(instance_dir)
    count = recover_missions(instance_dir, dry_run=dry_run)

    # Notify about escalated missions (needs_input) — read from the log
    log_path = Path(instance_dir) / "recovery.jsonl"
    escalated_msgs = []
    if log_path.exists():
        try:
            with open(log_path) as f:
                for line in f:
                    try:
                        ev = json.loads(line)
                        if ev.get("action") == "escalated":
                            escalated_msgs.append(ev.get("mission", "?")[:80])
                    except json.JSONDecodeError:
                        pass
        except OSError:
            pass

    if count > 0 or has_pending or escalated_msgs:
        parts = []
        if count > 0:
            parts.append(f"{count} mission(s) moved back to Pending")
        if has_pending:
            parts.append("interrupted run detected (pending.md) — will resume")
        msg = "Restart — " + ", ".join(parts) + "." if parts else ""

        if escalated_msgs:
            escalated_summary = "; ".join(escalated_msgs[:3])
            if len(escalated_msgs) > 3:
                escalated_summary += f" (+{len(escalated_msgs) - 3} more)"
            needs_input_msg = (
                f"⚠️ Recovery escalation: {len(escalated_msgs)} mission(s) failed "
                f"{MAX_RECOVERY_ATTEMPTS} recovery attempts and need human review:\n"
                f"{escalated_summary}"
            )
            format_and_send(needs_input_msg)
            print(f"[recover] {needs_input_msg}")

        if msg:
            format_and_send(msg)
            print(f"[recover] {msg}")
    else:
        print("[recover] No stale missions found")

    print(count)
