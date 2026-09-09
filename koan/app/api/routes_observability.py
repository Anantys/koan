"""REST API observability routes: usage, metrics, and log tails."""

from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from app.api.auth import require_token
from app.api.openapi_metadata import openapi_operation, query_parameter
from app.log_reader import LOG_DEFAULT_LIMIT, LOG_MAX_LIMIT

bp = Blueprint("observability", __name__)

_USAGE_DEFAULT_DAYS = 7
_USAGE_DEFAULT_OFFSET = 0
_USAGE_MIN_DAYS = 1
_USAGE_MAX_DAYS = 100
_METRICS_DEFAULT_DAYS = 30
_METRICS_MIN_DAYS = 0
_METRICS_MAX_DAYS = 365

_USAGE_QUERY_PARAMETERS = (
    query_parameter(
        "days",
        {
            "type": "integer",
            "default": _USAGE_DEFAULT_DAYS,
            "minimum": _USAGE_MIN_DAYS,
            "maximum": _USAGE_MAX_DAYS,
        },
        "Window length; values are clamped to the documented range.",
    ),
    query_parameter(
        "offset",
        {
            "type": "integer",
            "default": _USAGE_DEFAULT_OFFSET,
            "minimum": 0,
        },
        "Shift the window back by this many granularity units.",
    ),
    query_parameter(
        "granularity",
        {
            "type": "string",
            "enum": ["day", "week", "month"],
            "default": "day",
        },
        "Series bucketing granularity.",
    ),
    query_parameter(
        "stacked",
        {"type": "boolean", "default": False},
        "Include a per-project series breakdown.",
    ),
    query_parameter(
        "project",
        {"type": "string"},
        "Restrict totals and series to one project.",
    ),
)

_METRICS_QUERY_PARAMETERS = (
    query_parameter(
        "days",
        {
            "type": "integer",
            "default": _METRICS_DEFAULT_DAYS,
            "minimum": _METRICS_MIN_DAYS,
            "maximum": _METRICS_MAX_DAYS,
        },
        "Lookback window; values are clamped to the documented range.",
    ),
    query_parameter(
        "project",
        {"type": "string"},
        "Return metrics and trend for one project.",
    ),
)

_LOGS_QUERY_PARAMETERS = (
    query_parameter(
        "source",
        {
            "type": "string",
            "enum": ["run", "awake", "all"],
            "default": "all",
        },
        "Log source to read.",
    ),
    query_parameter(
        "limit",
        {
            "type": "integer",
            "default": LOG_DEFAULT_LIMIT,
            "minimum": 1,
            "maximum": LOG_MAX_LIMIT,
        },
        "Maximum lines returned per source.",
    ),
    query_parameter(
        "q",
        {"type": "string"},
        "Case-insensitive substring filter.",
    ),
)


def _instance_dir() -> Path:
    return current_app.config["INSTANCE_DIR"]


def _koan_root() -> Path:
    return current_app.config["KOAN_ROOT"]


class _BadParam(ValueError):
    """Raised when a query param fails to parse as an integer."""


def _int_param(name: str, default: str) -> int:
    """Parse an integer query param, raising _BadParam on malformed input."""
    raw = request.args.get(name, default)
    try:
        return int(raw)
    except (ValueError, TypeError):
        raise _BadParam(f"'{name}' must be an integer, got {raw!r}")


@bp.route("/v1/usage")
@openapi_operation(query_parameters=_USAGE_QUERY_PARAMETERS)
@require_token
def usage():
    from app.usage_service import build_usage_payload

    try:
        days = _int_param("days", str(_USAGE_DEFAULT_DAYS))
        offset = _int_param("offset", str(_USAGE_DEFAULT_OFFSET))
    except _BadParam as e:
        return jsonify({"error": {"code": "invalid_request", "message": str(e)}}), 422
    stacked = request.args.get("stacked", "false").lower() in ("true", "1", "yes")
    return jsonify(build_usage_payload(
        _instance_dir(),
        days=days,
        project=request.args.get("project", ""),
        granularity=request.args.get("granularity", "day"),
        stacked=stacked,
        offset=offset,
    ))


@bp.route("/v1/metrics")
@openapi_operation(query_parameters=_METRICS_QUERY_PARAMETERS)
@require_token
def metrics():
    from app.mission_metrics import (
        compute_global_metrics,
        compute_project_metrics,
        compute_project_trend,
    )

    try:
        days = max(
            _METRICS_MIN_DAYS,
            min(
                _int_param("days", str(_METRICS_DEFAULT_DAYS)),
                _METRICS_MAX_DAYS,
            ),
        )
    except _BadParam as e:
        return jsonify({"error": {"code": "invalid_request", "message": str(e)}}), 422
    project = request.args.get("project", "")
    instance = str(_instance_dir())

    if project:
        data = compute_project_metrics(instance, project, days=days)
        data["trend"] = compute_project_trend(instance, project, days=days)
        return jsonify(data)

    data = compute_global_metrics(instance, days=days)
    for proj, pdata in data.get("by_project", {}).items():
        if isinstance(pdata, dict):
            pdata["trend"] = compute_project_trend(instance, proj, days=days)
    from app.security_review import count_security_blocks
    data["security_blocks_7d"] = count_security_blocks(instance, days=7)
    return jsonify(data)


@bp.route("/v1/logs")
@openapi_operation(query_parameters=_LOGS_QUERY_PARAMETERS)
@require_token
def logs():
    from app.log_reader import read_logs

    source = request.args.get("source", "all")
    try:
        limit = _int_param("limit", str(LOG_DEFAULT_LIMIT))
    except _BadParam as e:
        return jsonify({"error": {"code": "invalid_request", "message": str(e)}}), 422
    q = request.args.get("q", "")
    return jsonify(read_logs(_koan_root(), source=source, limit=limit, q=q))
