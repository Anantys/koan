"""
Activity usage logger — per-action usage tracking with log rotation.

Logs human-readable usage entries to ``logs/usage.log`` so operators can see
how much effort each mission or activity consumed.  Uses Python's
``RotatingFileHandler`` (5 MB per file, 5 backups) for automatic rotation.

Each line records: timestamp, project, activity type, duration, token counts,
cost, and a short description.

Usage::

    from app.activity_usage_logger import log_activity_usage

    log_activity_usage(
        project="koan",
        activity_type="mission",
        description="Fix CORS headers",
        duration_seconds=342,
        input_tokens=12000,
        output_tokens=4500,
        cost_usd=0.042,
        model="claude-sonnet-4-20250514",
    )
"""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

_logger: Optional[logging.Logger] = None

# Rotation settings
_MAX_BYTES = 5 * 1024 * 1024  # 5 MB per file
_BACKUP_COUNT = 5              # keep 5 rotated copies


def _get_logger() -> logging.Logger:
    """Lazy-init the rotating file logger."""
    global _logger
    if _logger is not None:
        return _logger

    koan_root = os.environ.get("KOAN_ROOT", "")
    if not koan_root:
        # Fallback: try to infer from current working directory
        koan_root = os.getcwd()

    logs_dir = Path(koan_root) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    log_path = logs_dir / "usage.log"

    _logger = logging.getLogger("koan.activity_usage")
    _logger.setLevel(logging.INFO)
    _logger.propagate = False

    # Avoid duplicate handlers on re-init
    if not _logger.handlers:
        handler = RotatingFileHandler(
            str(log_path),
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        _logger.addHandler(handler)

    return _logger


def _format_duration(seconds: int) -> str:
    """Format seconds as human-readable duration."""
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    secs = seconds % 60
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours}h{mins:02d}m"


def _format_tokens(n: int) -> str:
    """Format token count compactly."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def log_activity_usage(
    project: str,
    activity_type: str,
    description: str = "",
    duration_seconds: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cost_usd: float = 0.0,
    model: str = "",
) -> None:
    """Log a single activity's usage to logs/usage.log.

    Args:
        project: Project name.
        activity_type: Type of activity (mission, contemplative, skill, etc.).
        description: Short description of what was done.
        duration_seconds: Wall-clock duration in seconds.
        input_tokens: Input tokens consumed.
        output_tokens: Output tokens produced.
        cache_read_tokens: Tokens read from prompt cache.
        cache_creation_tokens: Tokens written to prompt cache.
        cost_usd: Dollar cost reported by the API.
        model: Model identifier.
    """
    import time

    try:
        logger = _get_logger()
    except OSError:
        # If we can't create the log dir/file, silently skip
        return

    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    duration_str = _format_duration(duration_seconds) if duration_seconds > 0 else "-"
    total_tokens = input_tokens + output_tokens
    tokens_str = f"{_format_tokens(total_tokens)} tokens ({_format_tokens(input_tokens)} in / {_format_tokens(output_tokens)} out)"

    # Cache info (only if relevant)
    cache_str = ""
    if cache_read_tokens or cache_creation_tokens:
        cache_str = f" | cache: {_format_tokens(cache_read_tokens)} read, {_format_tokens(cache_creation_tokens)} created"

    # Cost info
    cost_str = ""
    if cost_usd > 0:
        cost_str = f" | ${cost_usd:.4f}"

    # Model info (shortened)
    model_str = ""
    if model:
        # Shorten common model names for readability
        short_model = model.replace("claude-", "").split("-2025")[0]
        model_str = f" | {short_model}"

    # Truncate description to keep lines readable
    desc = description[:80] if description else "-"

    line = (
        f"[{ts}] {project:<15} {activity_type:<14} "
        f"{duration_str:>8}  {tokens_str}{cache_str}{cost_str}{model_str}"
        f"  {desc}"
    )

    logger.info(line)


def reset() -> None:
    """Clear cached logger (for tests)."""
    global _logger
    if _logger is not None:
        for handler in _logger.handlers[:]:
            handler.close()
            _logger.removeHandler(handler)
    _logger = None
