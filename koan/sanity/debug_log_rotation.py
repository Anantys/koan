"""
Kōan — Debug log rotation sanity check.

Truncates .koan-debug.log when it exceeds a size threshold, keeping
the most recent lines. Prevents unbounded disk growth when debug
mode is enabled (``debug: true`` in config.yaml).

The debug log is append-only with no built-in rotation. On systems
that run continuously with debugging enabled, the file can grow to
gigabytes over time. This check runs at startup and caps the file
to the last MAX_KEEP_LINES lines when the size exceeds MAX_SIZE_BYTES.
"""

import os
from typing import List, Tuple


# Rotate when the file exceeds 10 MB
MAX_SIZE_BYTES = 10 * 1024 * 1024

# Keep the last 5000 lines after rotation
MAX_KEEP_LINES = 5000

DEBUG_LOG_FILENAME = ".koan-debug.log"


def _get_debug_log_path() -> str:
    """Return the path to .koan-debug.log."""
    koan_root = os.environ.get("KOAN_ROOT", "")
    if not koan_root:
        return ""
    return os.path.join(koan_root, DEBUG_LOG_FILENAME)


def rotate_debug_log(log_path: str) -> Tuple[bool, List[str]]:
    """Truncate the debug log if it exceeds the size threshold.

    Args:
        log_path: Absolute path to .koan-debug.log.

    Returns:
        (was_modified, list_of_changes)
    """
    if not log_path or not os.path.isfile(log_path):
        return False, []

    try:
        size = os.path.getsize(log_path)
    except OSError:
        return False, []

    if size <= MAX_SIZE_BYTES:
        return False, []

    # Read all lines, keep only the last MAX_KEEP_LINES
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return False, []

    original_count = len(lines)
    if original_count <= MAX_KEEP_LINES:
        return False, []

    kept = lines[-MAX_KEEP_LINES:]
    try:
        with open(log_path, "w", encoding="utf-8") as f:
            f.writelines(kept)
    except OSError:
        return False, []

    trimmed = original_count - MAX_KEEP_LINES
    size_mb = size / (1024 * 1024)
    return True, [
        f"Rotated .koan-debug.log: {size_mb:.1f}MB, "
        f"{original_count} lines → {MAX_KEEP_LINES} (trimmed {trimmed})"
    ]


def run(instance_dir: str) -> Tuple[bool, List[str]]:
    """Sanity runner interface: rotate debug log if oversized."""
    log_path = _get_debug_log_path()
    return rotate_debug_log(log_path)
