"""Shared usage-payload builder used by the dashboard and the REST API.

Holds the bucketing helpers and the full /api/usage payload computation so
both the dashboard process and the API process compute usage identically.
"""

import calendar as _calendar
from datetime import date, timedelta
from pathlib import Path

# The window clamp `build_usage_payload` enforces. Exported so the REST API
# publishes the bounds it actually applies instead of restating them.
USAGE_MIN_DAYS = 1
USAGE_MAX_DAYS = 100
USAGE_DEFAULT_DAYS = 7
USAGE_MIN_OFFSET = 0


def _empty_project_bucket() -> dict:
    return {
        "total_input": 0,
        "total_output": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "count": 0,
    }


def _recompute_cache_hit_rates(buckets: dict) -> None:
    from app.token_parser import compute_cache_hit_rate

    for b in buckets.values():
        b["cache_hit_rate"] = compute_cache_hit_rate(
            b["total_input"],
            b["cache_read_input_tokens"],
            b["cache_creation_input_tokens"],
        )
        if "by_project" in b:
            for bp in b["by_project"].values():
                bp["cache_hit_rate"] = compute_cache_hit_rate(
                    bp["total_input"],
                    bp["cache_read_input_tokens"],
                    bp["cache_creation_input_tokens"],
                )


def _bucket_by_week(series: list) -> list:
    """Aggregate daily series into ISO-week buckets."""
    buckets: dict = {}
    for entry in series:
        d = date.fromisoformat(entry["date"])
        iso_year, iso_week, _ = d.isocalendar()
        key = (iso_year, iso_week)
        if key not in buckets:
            monday = d - timedelta(days=d.weekday())
            sunday = monday + timedelta(days=6)
            bucket: dict = {
                "week": f"{iso_year}-W{iso_week:02d}",
                "date": monday.isoformat(),
                "start": monday.isoformat(),
                "end": sunday.isoformat(),
                "total_input": 0,
                "total_output": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "count": 0,
                "cost": None,
            }
            if "by_project" in entry:
                bucket["by_project"] = {}
            buckets[key] = bucket
        b = buckets[key]
        b["total_input"] += entry.get("total_input", 0)
        b["total_output"] += entry.get("total_output", 0)
        b["cache_creation_input_tokens"] += entry.get("cache_creation_input_tokens", 0)
        b["cache_read_input_tokens"] += entry.get("cache_read_input_tokens", 0)
        b["count"] += entry.get("count", 0)
        entry_cost = entry.get("cost")
        if entry_cost is not None:
            b["cost"] = (b["cost"] or 0.0) + entry_cost
        if "by_project" in entry and "by_project" in b:
            for proj, pdata in entry["by_project"].items():
                if proj not in b["by_project"]:
                    b["by_project"][proj] = _empty_project_bucket()
                bp = b["by_project"][proj]
                bp["total_input"] += pdata.get("total_input", 0)
                bp["total_output"] += pdata.get("total_output", 0)
                bp["cache_creation_input_tokens"] += pdata.get("cache_creation_input_tokens", 0)
                bp["cache_read_input_tokens"] += pdata.get("cache_read_input_tokens", 0)
                bp["count"] += pdata.get("count", 0)

    _recompute_cache_hit_rates(buckets)
    return [buckets[k] for k in sorted(buckets.keys())]


def _bucket_by_month(series: list) -> list:
    """Aggregate daily series into calendar-month buckets."""
    buckets: dict = {}
    for entry in series:
        d = date.fromisoformat(entry["date"])
        key = (d.year, d.month)
        if key not in buckets:
            bucket: dict = {
                "month": f"{d.year}-{d.month:02d}",
                "date": f"{d.year}-{d.month:02d}-01",
                "total_input": 0,
                "total_output": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "count": 0,
                "cost": None,
            }
            if "by_project" in entry:
                bucket["by_project"] = {}
            buckets[key] = bucket
        b = buckets[key]
        b["total_input"] += entry.get("total_input", 0)
        b["total_output"] += entry.get("total_output", 0)
        b["cache_creation_input_tokens"] += entry.get("cache_creation_input_tokens", 0)
        b["cache_read_input_tokens"] += entry.get("cache_read_input_tokens", 0)
        b["count"] += entry.get("count", 0)
        entry_cost = entry.get("cost")
        if entry_cost is not None:
            b["cost"] = (b["cost"] or 0.0) + entry_cost
        if "by_project" in entry and "by_project" in b:
            for proj, pdata in entry["by_project"].items():
                if proj not in b["by_project"]:
                    b["by_project"][proj] = _empty_project_bucket()
                bp = b["by_project"][proj]
                bp["total_input"] += pdata.get("total_input", 0)
                bp["total_output"] += pdata.get("total_output", 0)
                bp["cache_creation_input_tokens"] += pdata.get("cache_creation_input_tokens", 0)
                bp["cache_read_input_tokens"] += pdata.get("cache_read_input_tokens", 0)
                bp["count"] += pdata.get("count", 0)

    _recompute_cache_hit_rates(buckets)
    return [buckets[k] for k in sorted(buckets.keys())]


def _resolve_window(today: date, days: int, granularity: str, offset: int):
    """Return (start, end) for the requested window/granularity/offset."""
    if granularity == "week":
        end = today - timedelta(weeks=offset)
        start = end - timedelta(days=days - 1)
    elif granularity == "month":
        year, month = today.year, today.month
        month -= offset
        while month <= 0:
            month += 12
            year -= 1
        last_day = _calendar.monthrange(year, month)[1]
        end = date(year, month, min(today.day, last_day))
        start = end - timedelta(days=days - 1)
    else:
        end = today - timedelta(days=offset * days)
        start = end - timedelta(days=days - 1)
    return start, end


def build_usage_payload(
    instance_dir: Path,
    *,
    days: int = USAGE_DEFAULT_DAYS,
    project: str = "",
    granularity: str = "day",
    stacked: bool = False,
    offset: int = 0,
) -> dict:
    """Build the /api/usage JSON payload for the given instance dir and window.

    Mirrors the dashboard's /api/usage exactly so both endpoints stay in sync.
    """
    from app.cost_tracker import (
        summarize_range,
        get_pricing_config,
        estimate_cost,
        estimate_cache_savings,
        daily_series,
    )

    try:
        days = max(USAGE_MIN_DAYS, min(int(days), USAGE_MAX_DAYS))
    except (ValueError, TypeError):
        days = USAGE_DEFAULT_DAYS
    try:
        offset = max(USAGE_MIN_OFFSET, int(offset))
    except (ValueError, TypeError):
        offset = USAGE_MIN_OFFSET
    if granularity not in ("day", "week", "month"):
        granularity = "day"

    today = date.today()
    start, end = _resolve_window(today, days, granularity, offset)

    summary = summarize_range(instance_dir, start, end)

    by_project = summary["by_project"]
    if project and by_project:
        by_project = {k: v for k, v in by_project.items() if k == project}

    pricing = get_pricing_config()

    estimated_cost = None
    if pricing and summary["by_model"]:
        total_cost = 0.0
        for model_id, model_data in summary["by_model"].items():
            model_tokens = {
                "model": model_id,
                "input_tokens": model_data["input_tokens"],
                "output_tokens": model_data["output_tokens"],
            }
            c = estimate_cost(model_tokens, pricing)
            if c is not None:
                total_cost += c
                model_data["cost_usd"] = c
        estimated_cost = total_cost

    series = daily_series(
        instance_dir, start, end,
        project=project or None,
        include_by_project=stacked,
    )
    if granularity == "week":
        series = _bucket_by_week(series)
    elif granularity == "month":
        series = _bucket_by_month(series)

    estimated_cache_savings = estimate_cache_savings(summary, pricing)

    response_data: dict = {
        "days": days,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "total_input": summary["total_input"],
        "total_output": summary["total_output"],
        "cache_creation_input_tokens": summary["cache_creation_input_tokens"],
        "cache_read_input_tokens": summary["cache_read_input_tokens"],
        "cache_hit_rate": summary["cache_hit_rate"],
        "count": summary["count"],
        "by_project": by_project,
        "by_model": summary["by_model"],
        "has_pricing": pricing is not None,
        "estimated_cost": estimated_cost,
        "estimated_cache_savings": estimated_cache_savings,
        "series": series,
        "granularity": granularity,
        "offset": offset,
    }

    if project:
        response_data["by_type"] = summary.get("by_project_and_type", {}).get(project, {})
        response_data["by_mode"] = summary.get("by_project_and_mode", {}).get(project, {})
    else:
        response_data["by_type"] = summary.get("by_type", {})
        response_data["by_mode"] = summary.get("by_mode", {})
    response_data["by_project_and_type"] = summary.get("by_project_and_type", {})
    response_data["by_project_and_mode"] = summary.get("by_project_and_mode", {})

    return response_data
