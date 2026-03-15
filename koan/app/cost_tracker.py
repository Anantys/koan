"""
Kōan Cost Tracker — Structured per-model and per-project token tracking.

Records each API call as a JSONL line with model, project, input/output
tokens, and timestamp. Files are date-partitioned under instance/usage/.

Usage:
    from app.cost_tracker import record_usage

    record_usage(
        instance_dir=Path("/path/to/instance"),
        project="my-project",
        model="claude-sonnet-4-20250514",
        input_tokens=1500,
        output_tokens=500,
        mode="implement",
        mission="Fix the bug",
    )
"""

import fcntl
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional


def record_usage(
    instance_dir: Path,
    project: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    mode: str = "",
    mission: str = "",
) -> bool:
    """Append a usage event to today's JSONL file.

    Args:
        instance_dir: Path to instance directory.
        project: Project name (use "_global" for non-project sessions).
        model: Model identifier (e.g. "claude-sonnet-4-20250514").
        input_tokens: Number of input tokens consumed.
        output_tokens: Number of output tokens produced.
        mode: Autonomous mode (review/implement/deep).
        mission: Mission title or description.

    Returns:
        True if the record was written successfully.
    """
    usage_dir = Path(instance_dir) / "usage"
    usage_dir.mkdir(parents=True, exist_ok=True)

    today = date.today().isoformat()
    jsonl_path = usage_dir / f"{today}.jsonl"

    entry = {
        "ts": datetime.now().isoformat(),
        "project": project or "_global",
        "model": model or "unknown",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "mode": mode,
        "mission": mission,
    }
    line = json.dumps(entry, separators=(",", ":")) + "\n"

    try:
        with open(jsonl_path, "a", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        return True
    except OSError:
        return False


def _read_jsonl_for_date(usage_dir: Path, d: date) -> list:
    """Read and parse all valid JSONL lines for a given date."""
    jsonl_path = usage_dir / f"{d.isoformat()}.jsonl"
    if not jsonl_path.exists():
        return []
    entries = []
    try:
        for line in jsonl_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return entries


def _read_jsonl_range(usage_dir: Path, start: date, end: date) -> list:
    """Read all JSONL entries for a date range (inclusive)."""
    entries = []
    current = start
    while current <= end:
        entries.extend(_read_jsonl_for_date(usage_dir, current))
        current += timedelta(days=1)
    return entries


def summarize_day(instance_dir: Path, d: Optional[date] = None) -> dict:
    """Summarize usage for a single day.

    Returns:
        Dict with total_input, total_output, by_project, by_model.
    """
    if d is None:
        d = date.today()
    usage_dir = Path(instance_dir) / "usage"
    entries = _read_jsonl_for_date(usage_dir, d)
    return _aggregate(entries)


def summarize_range(instance_dir: Path, start: date, end: date) -> dict:
    """Summarize usage for a date range (inclusive)."""
    usage_dir = Path(instance_dir) / "usage"
    entries = _read_jsonl_range(usage_dir, start, end)
    return _aggregate(entries)


def summarize_by_project(instance_dir: Path, days: int = 7) -> dict:
    """Get per-project token breakdown for the last N days.

    Returns:
        Dict mapping project name to {input_tokens, output_tokens, count}.
    """
    end = date.today()
    start = end - timedelta(days=days - 1)
    summary = summarize_range(instance_dir, start, end)
    return summary["by_project"]


def summarize_by_model(instance_dir: Path, days: int = 7) -> dict:
    """Get per-model token breakdown for the last N days.

    Returns:
        Dict mapping model name to {input_tokens, output_tokens, count}.
    """
    end = date.today()
    start = end - timedelta(days=days - 1)
    summary = summarize_range(instance_dir, start, end)
    return summary["by_model"]


def _aggregate(entries: list) -> dict:
    """Aggregate a list of usage entries into a summary.

    Returns:
        Dict with keys: total_input, total_output, count,
        by_project (dict), by_model (dict).
    """
    result = {
        "total_input": 0,
        "total_output": 0,
        "count": 0,
        "by_project": {},
        "by_model": {},
    }

    for entry in entries:
        inp = entry.get("input_tokens", 0)
        out = entry.get("output_tokens", 0)
        project = entry.get("project", "_global")
        model = entry.get("model", "unknown")

        result["total_input"] += inp
        result["total_output"] += out
        result["count"] += 1

        # By project
        if project not in result["by_project"]:
            result["by_project"][project] = {"input_tokens": 0, "output_tokens": 0, "count": 0}
        result["by_project"][project]["input_tokens"] += inp
        result["by_project"][project]["output_tokens"] += out
        result["by_project"][project]["count"] += 1

        # By model
        if model not in result["by_model"]:
            result["by_model"][model] = {"input_tokens": 0, "output_tokens": 0, "count": 0}
        result["by_model"][model]["input_tokens"] += inp
        result["by_model"][model]["output_tokens"] += out
        result["by_model"][model]["count"] += 1

    return result


def estimate_cost(tokens: dict, pricing: Optional[dict] = None) -> Optional[float]:
    """Estimate dollar cost from a token breakdown dict.

    Args:
        tokens: Dict with input_tokens and output_tokens.
        pricing: Optional pricing table from config. Keys are model short
            names (opus/sonnet/haiku), values are dicts with "input" and
            "output" prices per million tokens. If None, returns None.

    Returns:
        Estimated cost in dollars, or None if no pricing available.
    """
    if not pricing:
        return None

    model = tokens.get("model", "unknown")
    model_lower = model.lower()

    # Match model to pricing table
    price = None
    for key in pricing:
        if key.lower() in model_lower:
            price = pricing[key]
            break

    if not price:
        return None

    inp_cost = tokens.get("input_tokens", 0) / 1_000_000 * price.get("input", 0)
    out_cost = tokens.get("output_tokens", 0) / 1_000_000 * price.get("output", 0)
    return inp_cost + out_cost


def daily_series(
    instance_dir: Path,
    start: date,
    end: date,
    project: Optional[str] = None,
) -> list:
    """Return per-day token breakdown for a date range.

    Args:
        instance_dir: Path to instance directory.
        start: Start date (inclusive).
        end: End date (inclusive).
        project: Optional project name to filter by.

    Returns:
        List of dicts, one per day: {date, total_input, total_output, count, cost}.
        cost is a float (USD) when pricing is configured, otherwise None.
    """
    usage_dir = Path(instance_dir) / "usage"
    pricing = get_pricing_config()
    result = []
    current = start
    while current <= end:
        entries = _read_jsonl_for_date(usage_dir, current)
        if project:
            entries = [e for e in entries if e.get("project") == project]
        day_summary = _aggregate(entries)

        # Estimate cost by summing per-model costs
        cost = None
        if pricing and day_summary["by_model"]:
            total_cost = 0.0
            for model_id, model_data in day_summary["by_model"].items():
                model_tokens = {
                    "model": model_id,
                    "input_tokens": model_data["input_tokens"],
                    "output_tokens": model_data["output_tokens"],
                }
                c = estimate_cost(model_tokens, pricing)
                if c is not None:
                    total_cost += c
            cost = total_cost

        result.append({
            "date": current.isoformat(),
            "total_input": day_summary["total_input"],
            "total_output": day_summary["total_output"],
            "count": day_summary["count"],
            "cost": cost,
        })
        current += timedelta(days=1)
    return result


def get_pricing_config(config: Optional[dict] = None) -> Optional[dict]:
    """Get pricing table from config.yaml → usage.pricing.

    Returns None if no pricing is configured.
    """
    if config is None:
        try:
            from app.utils import load_config
            config = load_config()
        except Exception as e:
            print(f"[cost_tracker] failed to load config: {e}", file=sys.stderr)
            return None
    pricing = config.get("usage", {}).get("pricing")
    if isinstance(pricing, dict):
        return pricing
    return None
