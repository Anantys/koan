"""REST API mission routes."""

import re
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from app.api.auth import require_token
from app.api.openapi_metadata import openapi_operation, query_parameter
from app.api.mission_index import (
    _normalize_for_match,
    cancel_mission,
    get_mission,
    list_missions,
    load_full_result,
    record_mission,
    reconcile,
    update_mission_text,
)

bp = Blueprint("missions", __name__)

# Validate command-style missions
_COMMAND_RE = re.compile(r"^/[a-zA-Z0-9_]+")

_CREATE_MISSION_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "description": "Slash-command mission; takes precedence over text.",
        },
        "text": {
            "type": "string",
            "description": "Free-form mission text.",
        },
        "project": {
            "type": "string",
            "description": "Optional project name added as a project tag.",
        },
        "urgent": {
            "type": "boolean",
            "default": False,
            "description": "Insert the mission at the front of the pending queue.",
        },
    },
    "anyOf": [
        {
            "required": ["command"],
            "properties": {"command": {"pattern": r"\S"}},
        },
        {
            "required": ["text"],
            "properties": {"text": {"pattern": r"\S"}},
        },
    ],
}

_REORDER_MISSION_SCHEMA = {
    "type": "object",
    "required": ["mission_id", "target_position"],
    "properties": {
        "mission_id": {
            "type": "string",
            "pattern": r"\S",
            "description": "Identifier of the pending mission to move.",
        },
        "target_position": {
            "type": "integer",
            "minimum": 1,
            "description": "One-indexed position in the pending queue.",
        },
    },
}

_EDIT_MISSION_SCHEMA = {
    "type": "object",
    "required": ["text"],
    "properties": {
        "text": {"type": "string", "pattern": r"\S"},
    },
}

_LIST_MISSIONS_QUERY_PARAMETERS = (
    query_parameter(
        "status",
        {
            "type": "string",
            "enum": ["pending", "in_progress", "done", "failed", "removed"],
        },
        "Restrict results to one mission status.",
    ),
    query_parameter(
        "project",
        {"type": "string"},
        "Restrict results to one project.",
    ),
)

_MISSION_ID_DESCRIPTION = "Mission identifier returned when the mission was queued."


def _instance_dir() -> Path:
    return current_app.config["INSTANCE_DIR"]


def _missions_file() -> Path:
    return _instance_dir() / "missions.md"


def _validate_mission_body(data: dict):
    """Validate POST /v1/missions request body.

    Returns (text, project, urgent) or raises ValueError.
    """
    command = data.get("command", "").strip()
    text = data.get("text", "").strip()

    if not command and not text:
        raise ValueError("One of 'command' or 'text' is required")

    mission_text = command or text

    # Sanitize
    from app.missions import sanitize_mission_text
    mission_text = sanitize_mission_text(mission_text)

    if not mission_text:
        raise ValueError("Mission text cannot be empty after sanitization")

    project = data.get("project", "").strip() or None
    urgent = bool(data.get("urgent", False))

    return mission_text, project, urgent


def _build_entry(text: str, project: str | None) -> str:
    """Build the missions.md list entry with optional project tag."""
    if project:
        return f"- [project:{project}] {text}"
    return f"- {text}"


def _find_pending_position(content: str, stored_text: str):
    """Find 1-indexed position of a mission in the pending section.

    Accepts raw missions.md content so callers can use it inside
    modify_missions_file() transforms (avoids TOCTOU races).
    Returns position when exactly one match exists; raises ValueError
    on duplicate matches to avoid mutating the wrong item.
    """
    from app.missions import parse_sections
    sections = parse_sections(content)
    needle = _normalize_for_match(stored_text)
    matches = [
        i
        for i, item in enumerate(sections.get("pending", []), 1)
        if _normalize_for_match(item) == needle
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError("Ambiguous match: multiple pending missions with identical text")
    return None


@bp.route("/v1/missions", methods=["GET"])
@openapi_operation(
    query_parameters=_LIST_MISSIONS_QUERY_PARAMETERS,
    mcp=True,
    mcp_description=(
        "Use this to browse or filter queues. Do not poll it for one mission's "
        "completion; use `koan_missions_get` with that mission id."
    ),
)
@require_token
def list_missions_route():
    """List missions, newest first, optionally filtered.

    Status and project filters narrow the response. Each record is reconciled
    against `missions.md` before it is returned.
    """
    status_filter = request.args.get("status")
    project_filter = request.args.get("project")
    records = list_missions(_instance_dir(), status_filter, project_filter)
    # Reconcile each record
    out = []
    for rec in records:
        rec = reconcile(_instance_dir(), _missions_file(), rec["id"])
        if rec:
            out.append(rec)
    return jsonify(out)


@bp.route("/v1/missions", methods=["POST"])
@openapi_operation(
    request_schema=_CREATE_MISSION_SCHEMA,
    mcp=True,
    mcp_description=(
        "Supply exactly one of `command` or `text`; a call with neither is "
        "rejected. The returned id identifies queued work, not a completed "
        "result. Poll `koan_missions_get`, then call "
        "`koan_missions_result` after status becomes `done`."
    ),
)
@require_token
def create_mission():
    """Queue a new mission.

    The mission is appended to the pending queue for a later agent cycle.
    Setting `urgent` inserts it at the front instead.
    """
    data = request.get_json(silent=True) or {}
    try:
        text, project, urgent = _validate_mission_body(data)
    except ValueError as e:
        return jsonify({"error": {"code": "invalid_request", "message": str(e)}}), 422

    entry = _build_entry(text, project)

    from app.utils import insert_pending_mission
    insert_pending_mission(_missions_file(), entry, urgent=urgent)

    mission_id = record_mission(_instance_dir(), entry, project)
    return jsonify({"id": mission_id, "status": "pending"}), 202


@bp.route("/v1/missions/reorder", methods=["POST"])
@openapi_operation(
    request_schema=_REORDER_MISSION_SCHEMA,
    mcp=True,
    mcp_description=(
        "Only pending missions can be reordered. Obtain the mission id from "
        "`koan_missions_list` and use a one-indexed target position."
    ),
)
@require_token
def reorder_mission_route():
    """Move a pending mission to a new position.

    Reordering changes queue priority without editing the mission text.
    """
    data = request.get_json(silent=True)
    if data is None:
        return jsonify(
            {"error": {"code": "invalid_request", "message": "Invalid JSON body"}}
        ), 422
    mission_id = data.get("mission_id", "").strip() if isinstance(data.get("mission_id"), str) else ""
    target_position = data.get("target_position")

    if not mission_id or target_position is None:
        return jsonify(
            {"error": {"code": "invalid_request", "message": "'mission_id' and 'target_position' are required"}}
        ), 422

    if isinstance(target_position, bool) or not isinstance(target_position, int):
        return jsonify(
            {"error": {"code": "invalid_request", "message": "'target_position' must be an integer"}}
        ), 422

    rec = get_mission(_instance_dir(), mission_id)
    if rec is None:
        return jsonify({"error": {"code": "not_found", "message": "Mission not found"}}), 404

    rec = reconcile(_instance_dir(), _missions_file(), mission_id)
    status = rec.get("status")

    if status != "pending":
        return jsonify(
            {"error": {"code": "conflict", "message": f"Cannot reorder mission in status '{status}'"}}
        ), 409

    from app.missions import reorder_mission
    from app.utils import modify_missions_file

    stored_text = rec.get("text", "")

    def transform(content):
        position = _find_pending_position(content, stored_text)
        if position is None:
            raise ValueError("Mission not found in pending queue")
        new_content, _ = reorder_mission(content, position, target_position)
        return new_content

    try:
        modify_missions_file(_missions_file(), transform)
    except ValueError as e:
        msg = str(e)
        if "not found in pending" in msg or "Ambiguous match" in msg:
            return jsonify({"error": {"code": "conflict", "message": msg}}), 409
        return jsonify({"error": {"code": "invalid_request", "message": msg}}), 422

    return jsonify({"id": mission_id, "status": "pending"}), 200


@bp.route("/v1/missions/<mission_id>", methods=["GET"])
@openapi_operation(
    mcp=True,
    mcp_description=(
        "Use this to poll one queued mission. When its status is `done`, "
        "fetch the complete structured result with `koan_missions_result`."
    ),
    path_parameter_descriptions={
        "mission_id": _MISSION_ID_DESCRIPTION,
    },
)
@require_token
def get_mission_route(mission_id: str):
    """Fetch one mission by id.

    Returns lifecycle state, outcome metadata, result references, and
    aggregated usage for the selected mission.
    """
    rec = get_mission(_instance_dir(), mission_id)
    if rec is None:
        return jsonify({"error": {"code": "not_found", "message": "Mission not found"}}), 404
    rec = reconcile(_instance_dir(), _missions_file(), mission_id)
    rec.setdefault("result", None)
    rec.setdefault("result_ref", None)
    rec.setdefault("outcome", None)

    from datetime import date, datetime
    from app.cost_tracker import aggregate_mission_usage

    start = None
    created = rec.get("created")
    if isinstance(created, (int, float)) and created > 0:
        try:
            start = datetime.fromtimestamp(created).date()
        except (ValueError, OSError) as e:
            current_app.logger.warning(
                "usage window: created=%r failed to convert for mission %s (%s); "
                "falling back to default window", created, mission_id, e,
            )
            start = None
    usage = aggregate_mission_usage(
        _instance_dir(), mission_id,
        mission_text=rec.get("text", ""),
        start=start, end=date.today(),
    )

    out = dict(rec)  # copy so the sidecar is never mutated with usage
    out["usage"] = usage
    return jsonify(out)


@bp.route("/v1/missions/<mission_id>/result", methods=["GET"])
@openapi_operation(
    mcp=True,
    mcp_description=(
        "Call this after `koan_missions_get` reports `done`. It returns the "
        "complete structured result and reports not found when no structured "
        "result is available."
    ),
    path_parameter_descriptions={
        "mission_id": _MISSION_ID_DESCRIPTION,
    },
)
@require_token
def get_mission_result_route(mission_id: str):
    """Fetch a finished mission's result.

    Inline and spilled results use the same HTTP response, so clients never
    need filesystem access.
    """
    if get_mission(_instance_dir(), mission_id) is None:
        return jsonify({"error": {"code": "not_found", "message": "Mission not found"}}), 404
    # reconcile so a just-completed mission gets its result attached first
    reconcile(_instance_dir(), _missions_file(), mission_id)
    result = load_full_result(_instance_dir(), mission_id)
    if result is None:
        return jsonify(
            {"error": {"code": "not_found", "message": "No structured result for this mission"}}
        ), 404
    return jsonify(result)


@bp.route("/v1/missions/<mission_id>", methods=["DELETE"])
@openapi_operation(
    mcp=True,
    mcp_description=(
        "This destructive tool is available only when "
        "`mcp.tools_allow_destructive` is enabled, and it accepts pending "
        "missions only."
    ),
    path_parameter_descriptions={
        "mission_id": _MISSION_ID_DESCRIPTION,
    },
)
@require_token
def delete_mission(mission_id: str):
    """Remove a pending mission.

    The mission is removed from the queue and its API record is cancelled.
    """
    rec = get_mission(_instance_dir(), mission_id)
    if rec is None:
        return jsonify({"error": {"code": "not_found", "message": "Mission not found"}}), 404

    # Reconcile first to get current status
    rec = reconcile(_instance_dir(), _missions_file(), mission_id)
    status = rec.get("status")

    if status != "pending":
        return jsonify(
            {"error": {"code": "conflict", "message": f"Cannot cancel mission in status '{status}'"}}
        ), 409

    # Remove from missions.md
    stored_text = rec.get("text", "")
    needle = _normalize_for_match(stored_text)

    def _remove(content: str) -> str:
        lines = content.splitlines(keepends=True)
        result = []
        for line in lines:
            if _normalize_for_match(line) == needle:
                continue
            result.append(line)
        return "".join(result)

    from app.utils import modify_missions_file
    modify_missions_file(_missions_file(), _remove)

    cancel_mission(_instance_dir(), mission_id)
    return jsonify({"id": mission_id, "status": "removed"}), 200


@bp.route("/v1/missions/<mission_id>", methods=["PATCH"])
@openapi_operation(request_schema=_EDIT_MISSION_SCHEMA)
@require_token
def edit_mission(mission_id: str):
    """Change a pending mission."""
    rec = get_mission(_instance_dir(), mission_id)
    if rec is None:
        return jsonify({"error": {"code": "not_found", "message": "Mission not found"}}), 404

    rec = reconcile(_instance_dir(), _missions_file(), mission_id)
    status = rec.get("status")

    if status != "pending":
        return jsonify(
            {"error": {"code": "conflict", "message": f"Cannot edit mission in status '{status}'"}}
        ), 409

    data = request.get_json(silent=True)
    if data is None:
        return jsonify(
            {"error": {"code": "invalid_request", "message": "Invalid JSON body"}}
        ), 422
    raw_text = data.get("text")
    if not isinstance(raw_text, str) or not raw_text.strip():
        return jsonify(
            {"error": {"code": "invalid_request", "message": "'text' is required and cannot be empty"}}
        ), 422
    new_text = raw_text.strip()

    from app.missions import sanitize_mission_text
    new_text = sanitize_mission_text(new_text)
    if not new_text:
        return jsonify(
            {"error": {"code": "invalid_request", "message": "Mission text cannot be empty after sanitization"}}
        ), 422

    from app.missions import edit_pending_mission
    from app.utils import modify_missions_file

    project = rec.get("project")
    edit_text = f"[project:{project}] {new_text}" if project else new_text
    stored_text = rec.get("text", "")

    def transform(content):
        position = _find_pending_position(content, stored_text)
        if position is None:
            raise ValueError("Mission not found in pending queue")
        new_content, _ = edit_pending_mission(content, position, edit_text)
        return new_content

    try:
        modify_missions_file(_missions_file(), transform)
    except ValueError as e:
        msg = str(e)
        if "not found in pending" in msg or "Ambiguous match" in msg:
            return jsonify({"error": {"code": "conflict", "message": msg}}), 409
        return jsonify({"error": {"code": "invalid_request", "message": msg}}), 422

    new_entry = _build_entry(new_text, project)
    if not update_mission_text(_instance_dir(), mission_id, new_entry):
        return jsonify(
            {"error": {"code": "conflict", "message": "Failed to update mission index"}}
        ), 409

    return jsonify({"id": mission_id, "status": "pending"}), 200
