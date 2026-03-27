#!/usr/bin/env python3
"""
Kōan -- Quota Exhaustion Handler

Detects quota exhaustion from CLI output (Claude, Copilot, etc.),
parses reset times, writes journal entries, and creates pause state.

Supports provider-specific error patterns:
- Claude: "out of extra usage", "quota reached", "resets 10am (TZ)"
- Copilot/GitHub: "too many requests", "HTTP 429", "Retry-After: N",
  "usage limit", "try again in X minutes"

Usage:
    python -m app.quota_handler check <koan_root> <instance> <project_name> <run_count> <stdout_file> <stderr_file>

Exit codes:
    0 = quota exhausted, pause created
    1 = no quota issue detected
"""

import os
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional, Tuple

# Strict patterns: specific enough to match safely in both stdout and stderr.
# These are actual CLI error messages, not terms that appear in normal text.
_STRICT_QUOTA_PATTERNS = [
    # Claude-specific error messages
    r"out of extra usage",
    r"quota.*reached",
]

# Loose patterns: generic terms that may appear in Claude's response text
# (e.g., a plan discussing API rate limiting).  Only safe to match in stderr.
_LOOSE_QUOTA_PATTERNS = [
    # Generic / shared
    r"rate limit",
    # Copilot / GitHub API
    r"too many requests",
    r"usage limit",
    r"exceeded.*(?:copilot|secondary).*(?:limit|rate)",
    r"copilot.*(?:not available|unavailable)",
    r"HTTP\s+429",
    r"status[\s:]+429",
    r"retry[\s-]+after",
]

# Combined list for backward-compatible use in detect_quota_exhaustion()
QUOTA_PATTERNS = _STRICT_QUOTA_PATTERNS + _LOOSE_QUOTA_PATTERNS

# Compiled regexes
_QUOTA_RE = re.compile("|".join(QUOTA_PATTERNS), re.IGNORECASE)
_STRICT_QUOTA_RE = re.compile("|".join(_STRICT_QUOTA_PATTERNS), re.IGNORECASE)
_LOOSE_QUOTA_RE = re.compile("|".join(_LOOSE_QUOTA_PATTERNS), re.IGNORECASE)

# Pattern to extract reset info from output.
# Claude: "resets 10am (Europe/Paris)"
# Copilot/GitHub: "Retry-After: 60" or "retry after 60 seconds" or "try again in X minutes"
_RESET_RE = re.compile(r"resets\s+.+", re.IGNORECASE)
_RETRY_AFTER_RE = re.compile(
    r"(?:retry[\s-]+after[\s:]+(\d+))|(?:try again in\s+(\d+)\s*(seconds?|minutes?|hours?))",
    re.IGNORECASE,
)

# Bounds for Retry-After values to prevent indefinite pauses from malformed API responses.
_MAX_RETRY_SECONDS = 86400  # 24 hours
_MAX_RETRY_MINUTES = 1440   # 24 hours in minutes
_MAX_RETRY_HOURS = 24       # 24 hours
_DEFAULT_RETRY_SECONDS = 3600  # 1 hour fallback for zero/negative values

# Sentinel returned when quota check is unreliable (both log files unreadable).
# Callers should check `result is QUOTA_CHECK_UNRELIABLE` to distinguish from
# "quota not exhausted" (None) and "quota exhausted" (tuple).
QUOTA_CHECK_UNRELIABLE = ("__unreliable__", "Quota check failed: could not read log files")


def _clamp_retry_seconds(seconds: int) -> int:
    """Clamp retry seconds to sane bounds.

    Zero or negative values are treated as unknown and default to 1 hour.
    Values above 24 hours are capped to 24 hours.
    """
    if seconds <= 0:
        return _DEFAULT_RETRY_SECONDS
    return min(seconds, _MAX_RETRY_SECONDS)


def detect_quota_exhaustion(text: str) -> bool:
    """Check if text contains quota exhaustion signals.

    Works across providers (Claude, Copilot, etc.) by matching
    a shared set of rate-limit and quota patterns.

    Args:
        text: Combined stdout + stderr from CLI execution

    Returns:
        True if quota exhaustion detected
    """
    return bool(_QUOTA_RE.search(text))


def extract_reset_info(text: str) -> str:
    """Extract the reset info string from CLI output.

    Supports both Claude-style ("resets 10am") and Copilot/GitHub-style
    ("Retry-After: 60", "try again in 5 minutes") reset info.

    Args:
        text: Combined output text

    Returns:
        Reset info string or empty string
    """
    # Claude-style: "resets 10am (Europe/Paris)"
    match = _RESET_RE.search(text)
    if match:
        return match.group(0).strip()

    # Copilot/GitHub-style: "Retry-After: 60" or "try again in 5 minutes"
    retry_match = _RETRY_AFTER_RE.search(text)
    if retry_match:
        # "Retry-After: N" (seconds)
        if retry_match.group(1):
            seconds = _clamp_retry_seconds(int(retry_match.group(1)))
            return f"resets in {_seconds_to_human(seconds)}"
        # "try again in N unit"
        if retry_match.group(2) and retry_match.group(3):
            value = int(retry_match.group(2))
            unit = retry_match.group(3).lower()
            if unit.startswith("minute"):
                value = min(value, _MAX_RETRY_MINUTES)
                seconds = _clamp_retry_seconds(value * 60)
            elif unit.startswith("hour"):
                value = min(value, _MAX_RETRY_HOURS)
                seconds = _clamp_retry_seconds(value * 3600)
            else:
                seconds = _clamp_retry_seconds(value)
            return f"resets in {_seconds_to_human(seconds)}"
    return ""


def _seconds_to_human(seconds: int) -> str:
    """Convert seconds to a human-readable duration string."""
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    remaining = minutes % 60
    if remaining:
        return f"{hours}h {remaining}m"
    return f"{hours}h"


def parse_reset_time(reset_info: str) -> Tuple[Optional[int], str]:
    """Parse reset time from extracted info.

    Delegates to reset_parser.py for the actual parsing.

    Args:
        reset_info: Raw reset string from Claude output

    Returns:
        (unix_timestamp, display_string) — timestamp is None if parsing fails
    """
    from app.reset_parser import parse_reset_time as _parse

    return _parse(reset_info)


def compute_resume_info(
    reset_timestamp: Optional[int],
    reset_display: str,
) -> Tuple[int, str]:
    """Compute the resume timestamp and message.

    Args:
        reset_timestamp: Parsed UNIX timestamp (or None if parsing failed)
        reset_display: Human-readable reset info

    Returns:
        (effective_timestamp, resume_message)
    """
    if reset_timestamp is not None:
        from app.reset_parser import time_until_reset

        until = time_until_reset(reset_timestamp)
        return reset_timestamp, f"Auto-resume at reset time (~{until})"

    # Fallback: current time + 1h retry
    from app.pause_manager import QUOTA_RETRY_SECONDS
    fallback_ts = int(datetime.now().timestamp()) + QUOTA_RETRY_SECONDS
    return fallback_ts, "Auto-resume in ~1h (reset time unknown)"


def write_quota_journal(
    instance_dir: str,
    project_name: str,
    run_count: int,
    reset_display: str,
    resume_message: str,
) -> None:
    """Write a quota exhaustion entry to the project journal.

    Args:
        instance_dir: Path to instance directory
        project_name: Current project name
        run_count: Number of runs completed
        reset_display: Human-readable reset info
        resume_message: Auto-resume message
    """
    from pathlib import Path
    from app.journal import append_to_journal

    now = datetime.now().strftime("%H:%M:%S")
    entry = f"""
## Quota Exhausted — {now}

Quota reached after {run_count} runs (project: {project_name}). {reset_display}

{resume_message} or use `/resume` to restart manually.
"""

    append_to_journal(Path(instance_dir), project_name, entry)


def handle_quota_exhaustion(
    koan_root: str,
    instance_dir: str,
    project_name: str,
    run_count: int,
    stdout_file: str,
    stderr_file: str,
) -> Optional[Tuple[str, str]]:
    """Full quota exhaustion handler.

    Checks CLI output for quota signals, parses reset time,
    writes journal, and creates pause state.  Works for any
    provider (Claude, Copilot, etc.).

    Args:
        koan_root: Path to koan root directory
        instance_dir: Path to instance directory
        project_name: Current project name
        run_count: Number of completed runs
        stdout_file: Path to CLI stdout capture file
        stderr_file: Path to CLI stderr capture file

    Returns:
        (reset_display, resume_message) if quota exhausted, None otherwise
    """
    # Read output files separately — stderr is trusted (CLI error messages),
    # stdout may contain Claude's response text which can mention "rate limit"
    # etc. in normal discussion (e.g., a plan about API rate limiting).
    stderr_text = ""
    stdout_text = ""
    read_failures = 0
    try:
        stderr_text = Path(stderr_file).read_text()
    except OSError:
        read_failures += 1
    try:
        stdout_text = Path(stdout_file).read_text()
    except OSError:
        read_failures += 1
    if read_failures == 2:
        print(
            f"[quota_handler] WARNING: could not read stdout ({stdout_file}) "
            f"or stderr ({stderr_file}) — quota check unreliable",
            file=sys.stderr,
        )
        return QUOTA_CHECK_UNRELIABLE

    # Check stderr with ALL patterns (both strict and loose) — stderr
    # contains CLI error messages, not user content.
    # Check stdout with STRICT patterns only — loose patterns like
    # "rate limit" cause false positives when Claude's response discusses
    # API rate limiting.
    quota_detected = bool(_QUOTA_RE.search(stderr_text)) or bool(
        _STRICT_QUOTA_RE.search(stdout_text)
    )
    if not quota_detected:
        return None

    # Extract and parse reset info (from both sources — reset times are safe)
    combined = stderr_text + "\n" + stdout_text
    reset_info = extract_reset_info(combined)
    reset_timestamp, reset_display = parse_reset_time(reset_info)
    effective_ts, resume_message = compute_resume_info(reset_timestamp, reset_display)

    # Write journal entry
    write_quota_journal(
        instance_dir, project_name, run_count, reset_display, resume_message
    )

    # Create pause state
    from app.pause_manager import create_pause

    create_pause(koan_root, "quota", effective_ts, reset_display)

    return reset_display, resume_message


_CLI_USAGE = "Usage: quota_handler.py check <koan_root> <instance> <project_name> <run_count> <stdout_file> <stderr_file>"


# CLI interface
if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] != "check":
        print(_CLI_USAGE, file=sys.stderr)
        sys.exit(1)

    if len(sys.argv) < 8:
        print(_CLI_USAGE, file=sys.stderr)
        sys.exit(1)

    try:
        run_count = int(sys.argv[5])
    except ValueError:
        run_count = 0

    result = handle_quota_exhaustion(
        koan_root=sys.argv[2],
        instance_dir=sys.argv[3],
        project_name=sys.argv[4],
        run_count=run_count,
        stdout_file=sys.argv[6],
        stderr_file=sys.argv[7],
    )
    if result is QUOTA_CHECK_UNRELIABLE:
        print("UNRELIABLE: could not read log files", file=sys.stderr)
        sys.exit(2)
    elif result:
        reset_display, resume_message = result
        # Output for bash: RESET_DISPLAY|RESUME_MSG
        print(f"{reset_display}|{resume_message}")
        sys.exit(0)
    else:
        sys.exit(1)
