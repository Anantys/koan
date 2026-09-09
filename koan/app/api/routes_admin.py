"""REST API admin routes (pause/resume, config, restart/shutdown/update)."""

import time
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from app.api.auth import require_token
from app.api.openapi_metadata import openapi_operation

bp = Blueprint("admin", __name__)

# Substrings that, when found in a config key name, indicate the value is secret.
# Matched via `s in key.lower()` — order doesn't matter.
_SECRET_SUBSTRINGS = (
    "token",
    "password",
    "secret",
    "api_key",
    "private_key",
    "access_key",
    "credential",
    "passphrase",
    "signing_key",
    "encryption_key",
)

_PAUSE_SCHEMA = {
    "type": "object",
    "properties": {
        "duration": {
            "type": "string",
            "description": "Duration such as 2h, 30m, or 1h30m; omit for indefinite.",
        },
    },
}


def _koan_root() -> Path:
    return current_app.config["KOAN_ROOT"]


def _instance_dir() -> Path:
    return current_app.config["INSTANCE_DIR"]


def _mask_secrets(obj, depth: int = 0):
    """Recursively mask secret values in a config dict."""
    if depth > 10:
        return obj
    if isinstance(obj, dict):
        return {
            k: (
                "***"
                if any(s in k.lower() for s in _SECRET_SUBSTRINGS)
                else _mask_secrets(v, depth + 1)
            )
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_mask_secrets(item, depth + 1) for item in obj]
    return obj


@bp.route("/v1/pause", methods=["POST"])
@openapi_operation(request_schema=_PAUSE_SCHEMA, request_required=False)
@require_token
def pause():
    data = request.get_json(silent=True) or {}
    duration_str = data.get("duration", "").strip()

    from app.pause_manager import create_pause, parse_duration
    timestamp = None
    display = ""

    if duration_str:
        secs = parse_duration(duration_str)
        if secs is None:
            return jsonify(
                {"error": {"code": "invalid_request", "message": "Invalid duration format. Use '2h', '30m', '1h30m'"}}
            ), 422
        timestamp = int(time.time()) + secs
        display = f"API pause ({duration_str})"

    create_pause(str(_koan_root()), "manual", timestamp=timestamp, display=display)
    return jsonify({"status": "paused", "duration": duration_str or None})


@bp.route("/v1/resume", methods=["POST"])
@require_token
def resume():
    from app.pause_manager import remove_pause
    remove_pause(str(_koan_root()))
    return jsonify({"status": "resumed"})


@bp.route("/v1/config", methods=["GET"])
@require_token
def get_config():
    from app.utils import load_config
    from app.utils import get_known_projects
    try:
        cfg = load_config()
    except Exception as e:
        return jsonify({"error": {"code": "config_error", "message": str(e)}}), 500

    masked = _mask_secrets(cfg)
    projects = [{"name": n, "path": p} for n, p in get_known_projects()]
    return jsonify({"config": masked, "projects": projects})


@bp.route("/v1/restart", methods=["POST"])
@require_token
def restart():
    # Route through request_restart() so both per-consumer markers are written
    # and the restart actually fires. Touching legacy .koan-restart was a
    # no-op — no consumer polls it.
    from app.restart_manager import request_restart
    try:
        request_restart(str(_koan_root()))
    except OSError as e:
        return jsonify({"error": {"code": "signal_error", "message": str(e)}}), 500
    return jsonify({"status": "restart_signaled"})


@bp.route("/v1/shutdown", methods=["POST"])
@require_token
def shutdown():
    from app.signals import STOP_FILE
    stop_file = _koan_root() / STOP_FILE
    try:
        stop_file.touch()
    except OSError as e:
        return jsonify({"error": {"code": "signal_error", "message": str(e)}}), 500
    return jsonify({"status": "shutdown_signaled"})


@bp.route("/v1/update", methods=["POST"])
@require_token
def update():
    try:
        from app.update_manager import check_update_safety, pull_upstream
        safety_msg = check_update_safety(_koan_root())
        if safety_msg:
            return jsonify({"error": {"code": "update_refused", "message": safety_msg}}), 409
        result = pull_upstream(_koan_root())
        from app.restart_manager import request_restart
        request_restart(str(_koan_root()))
        return jsonify({"status": "updated", "result": str(result)})
    except Exception as e:
        return jsonify({"error": {"code": "update_error", "message": str(e)}}), 500


@bp.route("/v1/update_release", methods=["POST"])
@require_token
def update_release():
    try:
        from app.update_manager import checkout_latest_tag
        result = checkout_latest_tag(_koan_root())
        if not result.success:
            return jsonify({
                "error": {"code": "update_failed", "message": result.error},
            }), 502
        from app.restart_manager import request_restart
        request_restart(str(_koan_root()))
        return jsonify({"status": "updated", "result": result.summary()})
    except Exception as e:
        return jsonify({"error": {"code": "update_error", "message": str(e)}}), 500
