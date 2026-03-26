"""Koan quota skill — live LLM quota check + manual override.

/quota        — show current quota metrics
/quota <N>    — override: tell koan it has N% remaining (fixes drift)
"""

import json
from datetime import datetime, timedelta
from pathlib import Path


SESSION_DURATION_HOURS = 5
DEFAULT_SESSION_LIMIT = 500_000
DEFAULT_WEEKLY_LIMIT = 5_000_000

# Claude CLI stats file (global, not per-instance)
STATS_CACHE_PATH = Path.home() / ".claude" / "stats-cache.json"


def handle(ctx):
    """Check LLM quota live, or override remaining % if argument given."""
    args = (ctx.args or "").strip()

    # --- Override mode: /quota <N> ---
    if args:
        return _handle_override(ctx, args)

    # --- Display mode: /quota ---
    return _handle_display(ctx)


def _handle_override(ctx, args):
    """Override internal quota estimation with human-provided remaining %."""
    try:
        remaining_pct = int(args)
    except ValueError:
        return f"Usage: /quota <remaining_%>\nExample: /quota 32 (= 32% remaining)"

    if remaining_pct < 0 or remaining_pct > 100:
        return "Remaining percentage must be between 0 and 100."

    instance_dir = ctx.instance_dir
    koan_root = ctx.koan_root
    state_file = instance_dir / "usage_state.json"
    usage_md = instance_dir / "usage.md"

    # Apply the override
    from app.usage_estimator import cmd_set_remaining
    cmd_set_remaining(remaining_pct, state_file, usage_md)

    # If paused for quota, clear the pause
    unpaused = False
    from app.pause_manager import is_paused, get_pause_state, remove_pause
    if is_paused(str(koan_root)):
        state = get_pause_state(str(koan_root))
        if state and state.is_quota:
            remove_pause(str(koan_root))
            unpaused = True

    # Confirm
    used_pct = 100 - remaining_pct
    msg = f"Quota override applied: {remaining_pct}% remaining ({used_pct}% used)."
    if unpaused:
        msg += "\nQuota pause cleared — agent will resume on next iteration."
    return msg


def _handle_display(ctx):
    """Show current quota metrics (original behavior)."""
    instance_dir = ctx.instance_dir
    koan_root = ctx.koan_root

    parts = []

    # --- Section 1: Koan's internal token tracking (live from state, not cache) ---
    state = _load_usage_state(instance_dir / "usage_state.json")
    config = _load_config()
    session_limit, weekly_limit = _get_limits(config)

    if state:
        state = _apply_resets(state)
        parts.append(_format_koan_usage(state, session_limit, weekly_limit))
    else:
        parts.append("No internal usage data yet (first run?).")

    # --- Section 2: Claude CLI stats (live from stats-cache.json) ---
    cli_stats = _load_cli_stats()
    if cli_stats:
        parts.append(_format_cli_stats(cli_stats))

    # --- Section 2b: Per-project/model breakdown (last 7 days) ---
    cost_section = _format_cost_breakdown(instance_dir)
    if cost_section:
        parts.append(cost_section)

    # --- Section 3: Agent state ---
    parts.append(_format_agent_state(koan_root))

    return "\n\n".join(parts)


def _load_usage_state(state_path):
    """Load raw usage state from JSON (not the cached usage.md)."""
    if not state_path.exists():
        return None
    try:
        return json.loads(state_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _load_config():
    """Load config.yaml for token limits."""
    try:
        from app.utils import load_config
        return load_config()
    except Exception:
        return {}


def _get_limits(config):
    """Get session/weekly token limits from config."""
    usage_cfg = config.get("usage", {})
    session_limit = usage_cfg.get("session_token_limit", DEFAULT_SESSION_LIMIT)
    weekly_limit = usage_cfg.get("weekly_token_limit", DEFAULT_WEEKLY_LIMIT)
    return session_limit, weekly_limit


def _apply_resets(state):
    """Apply session/weekly resets if windows have elapsed."""
    now = datetime.now()

    try:
        session_start = datetime.fromisoformat(state["session_start"])
    except (KeyError, ValueError):
        session_start = now
        state["session_start"] = now.isoformat()

    if (now - session_start).total_seconds() > SESSION_DURATION_HOURS * 3600:
        state["session_tokens"] = 0
        state["runs"] = 0
        state["session_start"] = now.isoformat()

    try:
        weekly_start = datetime.fromisoformat(state["weekly_start"])
    except (KeyError, ValueError):
        weekly_start = now
        state["weekly_start"] = now.isoformat()

    days_since = (now - weekly_start).days
    if days_since >= 7 or (days_since > 0 and now.weekday() < weekly_start.weekday()):
        state["weekly_tokens"] = 0
        state["weekly_start"] = now.isoformat()

    return state


def _format_tokens(n):
    """Format token count in human-friendly way."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _progress_bar(pct, width=10):
    """Build a small text progress bar."""
    filled = round(pct / 100 * width)
    filled = min(filled, width)
    empty = width - filled
    return "[" + "=" * filled + "." * empty + "]"


def _time_remaining(start_iso, duration_hours):
    """Calculate time remaining until reset."""
    try:
        start = datetime.fromisoformat(start_iso)
    except (ValueError, TypeError):
        return "?"
    reset_at = start + timedelta(hours=duration_hours)
    remaining = reset_at - datetime.now()
    if remaining.total_seconds() <= 0:
        return "now"
    hours = int(remaining.total_seconds() // 3600)
    minutes = int((remaining.total_seconds() % 3600) // 60)
    if hours > 0:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


def _format_koan_usage(state, session_limit, weekly_limit):
    """Format Koan's internal usage tracking."""
    session_tokens = state.get("session_tokens", 0)
    weekly_tokens = state.get("weekly_tokens", 0)
    runs = state.get("runs", 0)

    session_pct = min(100, int(session_tokens / max(1, session_limit) * 100))
    weekly_pct = min(100, int(weekly_tokens / max(1, weekly_limit) * 100))

    session_reset = _time_remaining(state.get("session_start"), SESSION_DURATION_HOURS)

    now = datetime.now()
    days_to_monday = (7 - now.weekday()) % 7
    if days_to_monday == 0:
        days_to_monday = 7

    lines = [
        "Session quota",
        f"  {_progress_bar(session_pct)} {session_pct}%",
        f"  {_format_tokens(session_tokens)} / {_format_tokens(session_limit)} tokens",
        f"  Resets in {session_reset} | {runs} run(s) this session",
        "",
        "Weekly quota",
        f"  {_progress_bar(weekly_pct)} {weekly_pct}%",
        f"  {_format_tokens(weekly_tokens)} / {_format_tokens(weekly_limit)} tokens",
        f"  Resets in {days_to_monday}d",
    ]

    return "\n".join(lines)


def _format_cost_breakdown(instance_dir):
    """Format per-project and per-model breakdown from JSONL cost data."""
    try:
        from app.cost_tracker import summarize_by_project, summarize_by_model, summarize_day
    except ImportError:
        return None

    by_project = summarize_by_project(instance_dir, days=7)
    by_model = summarize_by_model(instance_dir, days=7)

    if not by_project and not by_model:
        return None

    lines = ["Usage (7 days)"]

    if by_project:
        # Top 3 projects by total tokens
        sorted_projects = sorted(
            by_project.items(),
            key=lambda x: x[1]["input_tokens"] + x[1]["output_tokens"],
            reverse=True,
        )[:3]
        lines.append("  By project:")
        for name, data in sorted_projects:
            total = data["input_tokens"] + data["output_tokens"]
            lines.append(f"    {name}: {_format_tokens(total)} ({data['count']} runs)")

    if by_model:
        # Top 2 models by total tokens
        sorted_models = sorted(
            by_model.items(),
            key=lambda x: x[1]["input_tokens"] + x[1]["output_tokens"],
            reverse=True,
        )[:2]
        lines.append("  By model:")
        for name, data in sorted_models:
            short = _short_model_name(name)
            inp = data["input_tokens"]
            out = data["output_tokens"]
            lines.append(f"    {short}: {_format_tokens(inp)} in / {_format_tokens(out)} out")

    # Cache performance (today)
    today_summary = summarize_day(instance_dir)
    cache_read = today_summary.get("cache_read_input_tokens", 0)
    cache_create = today_summary.get("cache_creation_input_tokens", 0)
    cache_hit_rate = today_summary.get("cache_hit_rate", 0.0)
    total_cost = today_summary.get("total_cost_usd", 0.0)

    if cache_read or cache_create:
        lines.append("  Cache (today):")
        lines.append(f"    Hit rate: {cache_hit_rate:.0%}")
        lines.append(
            f"    Read: {_format_tokens(cache_read)} | "
            f"Created: {_format_tokens(cache_create)}"
        )
    if total_cost > 0:
        lines.append(f"  Cost (today): ${total_cost:.2f}")

    return "\n".join(lines)


def _load_cli_stats():
    """Load Claude CLI stats-cache.json (live, not Koan's cache)."""
    if not STATS_CACHE_PATH.exists():
        return None
    try:
        return json.loads(STATS_CACHE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _format_cli_stats(stats):
    """Format Claude CLI global statistics."""
    lines = ["Claude CLI stats"]

    # Today's activity
    today = datetime.now().strftime("%Y-%m-%d")
    daily = stats.get("dailyActivity", [])
    today_entry = next((d for d in daily if d.get("date") == today), None)

    if today_entry:
        msgs = today_entry.get("messageCount", 0)
        sessions = today_entry.get("sessionCount", 0)
        tools = today_entry.get("toolCallCount", 0)
        lines.append(f"  Today: {msgs:,} msgs | {sessions} sessions | {tools:,} tool calls")

    # Model token breakdown for today
    daily_tokens = stats.get("dailyModelTokens", [])
    today_tokens = next((d for d in daily_tokens if d.get("date") == today), None)

    if today_tokens:
        by_model = today_tokens.get("tokensByModel", {})
        if by_model:
            lines.append("  Today by model:")
            for model, tokens in sorted(by_model.items()):
                short_name = _short_model_name(model)
                lines.append(f"    {short_name}: {_format_tokens(tokens)}")

    # Total model usage (cumulative)
    model_usage = stats.get("modelUsage", {})
    if model_usage:
        lines.append("  Cumulative:")
        for model, usage in sorted(model_usage.items()):
            short_name = _short_model_name(model)
            inp = usage.get("inputTokens", 0)
            out = usage.get("outputTokens", 0)
            cache_read = usage.get("cacheReadInputTokens", 0)
            total = inp + out
            lines.append(
                f"    {short_name}: {_format_tokens(total)} "
                f"(+{_format_tokens(cache_read)} cache)"
            )

    # Total sessions
    total_sessions = stats.get("totalSessions", 0)
    total_messages = stats.get("totalMessages", 0)
    if total_sessions:
        lines.append(f"  All time: {total_sessions:,} sessions | {total_messages:,} messages")

    return "\n".join(lines)


def _short_model_name(model_id):
    """Shorten model ID to a friendly name."""
    if "opus" in model_id:
        return "Opus"
    if "sonnet" in model_id:
        return "Sonnet"
    if "haiku" in model_id:
        return "Haiku"
    return model_id.split("-")[1] if "-" in model_id else model_id


def _format_agent_state(koan_root):
    """Format current agent state."""
    lines = ["Agent"]

    pause_file = koan_root / ".koan-pause"
    stop_file = koan_root / ".koan-stop"

    if stop_file.exists():
        lines.append("  State: stopping")
    elif pause_file.exists():
        from app.pause_manager import get_pause_state
        state = get_pause_state(str(koan_root))
        reason = state.reason if state else ""
        if reason == "quota":
            lines.append("  State: paused (quota exhausted)")
        elif reason == "max_runs":
            lines.append("  State: paused (max runs)")
        else:
            lines.append("  State: paused")
    else:
        lines.append("  State: running")

    status_file = koan_root / ".koan-status"
    if status_file.exists():
        loop_status = status_file.read_text().strip()
        if loop_status:
            lines.append(f"  Loop: {loop_status}")

    return "\n".join(lines)
