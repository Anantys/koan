#!/usr/bin/env python3
"""
Kōan Usage Estimator — Track tokens from Claude JSON output, estimate usage %

Two commands:
    update <claude_json_output> <state_file> <usage_md>
        Parse Claude JSON output, accumulate tokens, refresh usage.md

    refresh <state_file> <usage_md>
        Recalculate usage.md from current state (session/weekly resets)

State is stored in instance/usage_state.json.
Writes usage.md in the same format as manual /usage paste.
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.utils import atomic_write, load_config


# Default limits (tokens). User tunes via config.yaml → usage.session_token_limit
DEFAULT_SESSION_LIMIT = 500_000
DEFAULT_WEEKLY_LIMIT = 5_000_000
SESSION_DURATION_HOURS = 5


def _load_state(state_file: Path) -> dict:
    """Load usage state from JSON file, or return fresh state."""
    if state_file.exists():
        try:
            return json.loads(state_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return _fresh_state()


def _fresh_state() -> dict:
    now = datetime.now().isoformat()
    return {
        "session_start": now,
        "session_tokens": 0,
        "weekly_start": now,
        "weekly_tokens": 0,
        "runs": 0,
    }


def _save_state(state_file: Path, state: dict):
    atomic_write(state_file, json.dumps(state, indent=2) + "\n")


def _maybe_reset(state: dict) -> dict:
    """Reset session/weekly counters if their windows have elapsed."""
    now = datetime.now()

    # Session reset: 5h since session_start
    try:
        session_start = datetime.fromisoformat(state["session_start"])
    except (KeyError, ValueError):
        session_start = now
        state["session_start"] = now.isoformat()

    if (now - session_start).total_seconds() > SESSION_DURATION_HOURS * 3600:
        state["session_start"] = now.isoformat()
        state["session_tokens"] = 0
        state["runs"] = 0

    # Weekly reset: Monday 00:00 boundary
    try:
        weekly_start = datetime.fromisoformat(state["weekly_start"])
    except (KeyError, ValueError):
        weekly_start = now
        state["weekly_start"] = now.isoformat()

    # If we've crossed a Monday since weekly_start
    days_since_start = (now - weekly_start).days
    if days_since_start >= 7 or (
        days_since_start > 0 and now.weekday() < weekly_start.weekday()
    ):
        state["weekly_start"] = now.isoformat()
        state["weekly_tokens"] = 0

    return state


def _extract_tokens(claude_json_path: Path) -> Optional[int]:
    """Extract total tokens from Claude --output-format json output.

    Tries multiple known field layouts:
    - Top-level: input_tokens + output_tokens
    - Nested: usage.input_tokens + usage.output_tokens
    - Array: sum across multiple turns

    Returns:
        Total token count (int) or None if no tokens found.
    """
    result = extract_tokens_detailed(claude_json_path)
    if result is None:
        return None
    return result["input_tokens"] + result["output_tokens"]


def extract_tokens_detailed(claude_json_path: Path) -> Optional[dict]:
    """Extract structured token info from Claude JSON output.

    Returns:
        Dict with keys: input_tokens, output_tokens, model,
        cache_creation_input_tokens, cache_read_input_tokens, cost_usd.
        None if no tokens found or file unreadable.
    """
    try:
        data = json.loads(claude_json_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    model = data.get("model", "unknown")

    # Try top-level fields
    inp = data.get("input_tokens", 0)
    out = data.get("output_tokens", 0)
    if inp or out:
        result = {"input_tokens": inp, "output_tokens": out, "model": model}
        _enrich_cache_fields(result, data)
        return result

    # Try nested usage object
    usage = data.get("usage", {})
    if isinstance(usage, dict):
        inp = usage.get("input_tokens", 0)
        out = usage.get("output_tokens", 0)
        if inp or out:
            result = {"input_tokens": inp, "output_tokens": out, "model": model}
            _enrich_cache_fields(result, data)
            return result

    # Try stats or metadata
    for key in ("stats", "metadata", "session"):
        sub = data.get(key, {})
        if isinstance(sub, dict):
            inp = sub.get("input_tokens", 0)
            out = sub.get("output_tokens", 0)
            if inp or out:
                result = {"input_tokens": inp, "output_tokens": out, "model": model}
                _enrich_cache_fields(result, data)
                return result

    return None


def _enrich_cache_fields(result: dict, data: dict) -> None:
    """Add cache token fields and cost_usd to an extracted token result.

    Searches for cache fields in:
    - Top-level usage object (snake_case: cache_creation_input_tokens)
    - modelUsage entries (camelCase: cacheCreationInputTokens)
    """
    cache_creation = 0
    cache_read = 0

    # Try nested usage object (snake_case — Claude CLI JSON format)
    usage = data.get("usage", {})
    if isinstance(usage, dict):
        cache_creation = usage.get("cache_creation_input_tokens", 0) or 0
        cache_read = usage.get("cache_read_input_tokens", 0) or 0

    # Fallback: modelUsage entries (camelCase — alternate format)
    if not cache_creation and not cache_read:
        model_usage = data.get("modelUsage", {})
        if isinstance(model_usage, dict):
            for model_data in model_usage.values():
                if isinstance(model_data, dict):
                    cache_creation += model_data.get("cacheCreationInputTokens", 0) or 0
                    cache_read += model_data.get("cacheReadInputTokens", 0) or 0

    result["cache_creation_input_tokens"] = cache_creation
    result["cache_read_input_tokens"] = cache_read

    # Extract cost_usd from top-level field (reported by Claude CLI)
    cost_usd = data.get("total_cost_usd")
    if cost_usd is not None and isinstance(cost_usd, (int, float)):
        result["cost_usd"] = round(cost_usd, 6)
    else:
        result["cost_usd"] = 0.0


def _get_limits(config: dict) -> tuple:
    """Get session/weekly token limits from config or defaults."""
    usage_cfg = config.get("usage", {})
    session_limit = usage_cfg.get("session_token_limit", DEFAULT_SESSION_LIMIT)
    weekly_limit = usage_cfg.get("weekly_token_limit", DEFAULT_WEEKLY_LIMIT)
    return session_limit, weekly_limit


def _estimate_reset_time(start_iso: str, duration_hours: float) -> str:
    """Estimate time remaining until reset."""
    try:
        start = datetime.fromisoformat(start_iso)
    except ValueError:
        return "unknown"
    now = datetime.now()
    reset_at = start + timedelta(hours=duration_hours)
    remaining = reset_at - now
    if remaining.total_seconds() <= 0:
        return "0m"
    hours = int(remaining.total_seconds() // 3600)
    minutes = int((remaining.total_seconds() % 3600) // 60)
    if hours > 0:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


def _write_usage_md(state: dict, usage_md: Path, config: dict):
    """Write usage.md in the standard format that usage_tracker.py can parse."""
    session_limit, weekly_limit = _get_limits(config)

    session_pct = min(100, int(state["session_tokens"] / max(1, session_limit) * 100))
    weekly_pct = min(100, int(state["weekly_tokens"] / max(1, weekly_limit) * 100))

    session_reset = _estimate_reset_time(state["session_start"], SESSION_DURATION_HOURS)
    # Weekly reset: days until next Monday
    now = datetime.now()
    days_to_monday = (7 - now.weekday()) % 7
    if days_to_monday == 0:
        days_to_monday = 7
    weekly_reset = f"{days_to_monday}d"

    # Fetch today's cache performance from cost tracker (best-effort)
    cache_line = _get_today_cache_line(usage_md.parent)

    content = f"""# Usage (estimated by koan)

Session (5hr) : {session_pct}% (reset in {session_reset})
Weekly (7 day) : {weekly_pct}% (Resets in {weekly_reset})
"""
    if cache_line:
        content += f"{cache_line}\n"

    content += f"""
<!-- Auto-generated by usage_estimator.py — {now.strftime('%Y-%m-%d %H:%M')} -->
<!-- Session tokens: {state['session_tokens']:,} / {session_limit:,} -->
<!-- Weekly tokens: {state['weekly_tokens']:,} / {weekly_limit:,} -->
<!-- Runs this session: {state.get('runs', 0)} -->
"""
    atomic_write(usage_md, content)


def _get_today_cache_line(instance_dir: Path) -> str:
    """Get today's cache performance summary from cost tracker.

    Returns a formatted line or empty string if unavailable.
    """
    try:
        from app.cost_tracker import format_cache_summary
        return format_cache_summary(instance_dir)
    except Exception as e:
        print(f"[usage_estimator] cache line fetch failed: {e}", file=sys.stderr)
        return ""


def cmd_update(claude_json_path: Path, state_file: Path, usage_md: Path):
    """Update state with tokens from a Claude run, then refresh usage.md."""
    config = load_config()
    state = _load_state(state_file)
    state = _maybe_reset(state)

    tokens = _extract_tokens(claude_json_path)
    if tokens is not None and tokens > 0:
        state["session_tokens"] = state.get("session_tokens", 0) + tokens
        state["weekly_tokens"] = state.get("weekly_tokens", 0) + tokens
        state["runs"] = state.get("runs", 0) + 1

    _save_state(state_file, state)
    _write_usage_md(state, usage_md, config)


def cmd_refresh(state_file: Path, usage_md: Path):
    """Refresh usage.md from current state (handles resets)."""
    config = load_config()
    state = _load_state(state_file)
    state = _maybe_reset(state)
    _save_state(state_file, state)
    _write_usage_md(state, usage_md, config)


def cmd_reset_session(state_file: Path, usage_md: Path):
    """Force-reset the session counters and refresh usage.md.

    Called when resuming from a quota pause — the human has verified
    that API quota is available, so the internal token counter should
    not block the next iteration with a stale high percentage.
    """
    config = load_config()
    state = _load_state(state_file)

    # Force session reset regardless of elapsed time
    state["session_start"] = datetime.now().isoformat()
    state["session_tokens"] = 0
    state["runs"] = 0

    _save_state(state_file, state)
    _write_usage_md(state, usage_md, config)


def cmd_reset_time(state_file: Path) -> int:
    """Compute when the current session resets (UNIX timestamp).

    Used by run.py to set a proper future pause timestamp when
    entering wait mode (budget exhausted).

    Returns:
        UNIX timestamp of the session reset time.
        Falls back to now + 5h if state is unreadable.
    """
    state = _load_state(state_file)
    try:
        session_start = datetime.fromisoformat(state["session_start"])
    except (KeyError, ValueError):
        return int((datetime.now() + timedelta(hours=SESSION_DURATION_HOURS)).timestamp())

    reset_at = session_start + timedelta(hours=SESSION_DURATION_HOURS)
    reset_ts = int(reset_at.timestamp())

    # If the computed reset time is in the past (stale state), use now + 5h
    now_ts = int(datetime.now().timestamp())
    if reset_ts <= now_ts:
        return now_ts + SESSION_DURATION_HOURS * 3600

    return reset_ts


def main():
    if len(sys.argv) < 2:
        print("Usage: usage_estimator.py <update|refresh|reset-time> ...", file=sys.stderr)
        sys.exit(1)

    command = sys.argv[1]

    if command == "update":
        if len(sys.argv) < 5:
            print("Usage: usage_estimator.py update <claude_json> <state_file> <usage_md>", file=sys.stderr)
            sys.exit(1)
        cmd_update(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]))

    elif command == "refresh":
        if len(sys.argv) < 4:
            print("Usage: usage_estimator.py refresh <state_file> <usage_md>", file=sys.stderr)
            sys.exit(1)
        cmd_refresh(Path(sys.argv[2]), Path(sys.argv[3]))

    elif command == "reset-session":
        if len(sys.argv) < 4:
            print("Usage: usage_estimator.py reset-session <state_file> <usage_md>", file=sys.stderr)
            sys.exit(1)
        cmd_reset_session(Path(sys.argv[2]), Path(sys.argv[3]))

    elif command == "reset-time":
        if len(sys.argv) < 3:
            print("Usage: usage_estimator.py reset-time <state_file>", file=sys.stderr)
            sys.exit(1)
        ts = cmd_reset_time(Path(sys.argv[2]))
        print(ts)

    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
