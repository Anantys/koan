"""Colored logging for the Koan agent loop.

Provides ANSI-colored log output with category-based color coding,
styled text helpers, and terminal reset for clean shutdown.

Categories:
  koan     (cyan)           — general agent messages
  error    (bold+red)       — errors
  init     (blue)           — startup messages
  health   (yellow)         — health checks, sanity
  git      (magenta)        — git operations
  github   (bold+magenta)   — GitHub operations
  mission  (green)          — mission lifecycle
  quota    (bold+yellow)    — quota/usage
  pause    (dim+blue)       — pause state
  warning  (yellow)         — warnings
  warn     (yellow)         — warnings (alias)

Usage:
    from app.run_log import log, bold_cyan
    log("init", "Starting up...")
    print(bold_cyan("=== Run 1/5 ==="))
"""

import os
import sys


# ---------------------------------------------------------------------------
# Color state
# ---------------------------------------------------------------------------

_COLORS = {}

# Standalone ANSI reset (no dependency on _COLORS initialization)
_ANSI_RESET = "\033[0m"


def _reset_terminal():
    """Write an ANSI reset to stdout and flush, restoring default attributes.

    Called on exit paths to ensure the terminal is not left with active
    ANSI attributes (DIM, BOLD, color, etc.) after Koan shuts down.
    """
    try:
        sys.stdout.write(_ANSI_RESET)
        sys.stdout.flush()
    except OSError:
        pass  # Terminal may be gone during shutdown


def _init_colors():
    """Initialize ANSI color codes based on TTY detection."""
    global _COLORS
    if os.environ.get("KOAN_FORCE_COLOR", "") or sys.stdout.isatty():
        _COLORS = {
            "reset": "\033[0m",
            "bold": "\033[1m",
            "dim": "\033[2m",
            "red": "\033[31m",
            "green": "\033[32m",
            "yellow": "\033[33m",
            "blue": "\033[34m",
            "magenta": "\033[35m",
            "cyan": "\033[36m",
            "white": "\033[37m",
        }
    else:
        _COLORS = {k: "" for k in [
            "reset", "bold", "dim", "red", "green", "yellow",
            "blue", "magenta", "cyan", "white",
        ]}


# ---------------------------------------------------------------------------
# Category → color mapping
# ---------------------------------------------------------------------------

_CATEGORY_COLORS = {
    "koan": "cyan",
    "error": "bold+red",
    "init": "blue",
    "health": "yellow",
    "git": "magenta",
    "github": "bold+magenta",
    "mission": "green",
    "quota": "bold+yellow",
    "pause": "dim+blue",
    "warning": "yellow",
    "warn": "yellow",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def log(category: str, message: str):
    """Print a colored log message."""
    if not _COLORS:
        _init_colors()
    color_spec = _CATEGORY_COLORS.get(category, "white")
    parts = color_spec.split("+")
    prefix = "".join(_COLORS.get(p, "") for p in parts)
    reset = _COLORS.get("reset", "")
    print(f"{prefix}[{category}]{reset} {message}", flush=True)


def _styled(text: str, *styles: str) -> str:
    """Apply ANSI styles to text. E.g. _styled("hi", "bold", "cyan")."""
    if not _COLORS:
        _init_colors()
    prefix = "".join(_COLORS.get(s, "") for s in styles)
    return f"{prefix}{text}{_COLORS.get('reset', '')}"


def bold_cyan(text: str) -> str:
    return _styled(text, "bold", "cyan")


def bold_green(text: str) -> str:
    return _styled(text, "bold", "green")
