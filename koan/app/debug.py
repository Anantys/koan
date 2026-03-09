"""Debug logging for mission execution visibility.

When ``debug: true`` is set in config.yaml, writes timestamped entries to
``.koan-debug.log`` at KOAN_ROOT. The file is append-only and opened/closed
per entry so ``tail -f`` works reliably.

When disabled (the default), ``debug_log()`` is a single boolean check with
zero file I/O.

Usage::

    from app.debug import debug_log
    debug_log(f"Dispatching skill: {command}")
"""

import os
import time
from pathlib import Path
from typing import Optional

from app.signals import DEBUG_LOG_FILE

_enabled: Optional[bool] = None
_log_path: Optional[Path] = None


def _init() -> None:
    """Lazy init: read debug flag from config and cache it."""
    global _enabled, _log_path

    try:
        from app.config import get_debug_enabled
        _enabled = get_debug_enabled()
    except (ImportError, OSError, ValueError):
        _enabled = False

    if _enabled:
        koan_root = os.environ.get("KOAN_ROOT", "")
        if koan_root:
            _log_path = Path(koan_root) / DEBUG_LOG_FILE
        else:
            _enabled = False


def debug_log(message: str) -> None:
    """Append a timestamped debug line. No-op when disabled."""
    if _enabled is None:
        _init()

    if not _enabled:
        return

    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(_log_path, "a") as f:
            f.write(f"[{ts}] {message}\n")
    except OSError:
        pass


def reset() -> None:
    """Clear cached state (for tests)."""
    global _enabled, _log_path
    _enabled = None
    _log_path = None
