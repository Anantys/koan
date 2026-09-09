"""REST API project routes."""

import logging
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from app.api.auth import require_token
from app.api.openapi_metadata import openapi_operation

log = logging.getLogger("koan.api")

bp = Blueprint("projects", __name__)

_ADD_PROJECT_SCHEMA = {
    "type": "object",
    "required": ["github_url"],
    "properties": {
        "github_url": {"type": "string", "pattern": r"\S"},
        "name": {"type": "string"},
    },
}

_PATCH_PROJECT_SCHEMA = {
    "type": "object",
    "required": ["patch"],
    "properties": {
        "patch": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "cli_provider": {"type": "string"},
                "autoreview": {"type": "boolean"},
                "focus": {"type": "boolean"},
                "exploration": {"type": "boolean"},
                "rtk": {"type": "boolean"},
                "devcontainer": {"type": "boolean"},
                "max_open_prs": {"type": "integer"},
                "max_pending_branches": {"type": "integer"},
                "github_url": {"type": "string"},
                "git_auto_merge.enabled": {"type": "boolean"},
                "git_auto_merge.base_branch": {"type": "string"},
                "git_auto_merge.strategy": {
                    "type": "string",
                    "enum": ["squash", "merge", "rebase"],
                },
            },
        },
    },
}


def _koan_root() -> Path:
    return current_app.config["KOAN_ROOT"]


def _instance_dir() -> Path:
    return current_app.config["INSTANCE_DIR"]


def _run_skill(command: str, args: str = "") -> tuple:
    """Run a skill in-process. Returns (ok: bool, result: str)."""
    result_parts = []

    def _send(msg: str) -> None:
        result_parts.append(msg)

    try:
        from app.bridge_state import _get_registry
        from app.skills import SkillContext, execute_skill

        registry = _get_registry()
        skill = registry.find_by_command(command)
        if skill is None:
            return False, f"Skill '{command}' not found"

        ctx = SkillContext(
            koan_root=_koan_root(),
            instance_dir=_instance_dir(),
            command_name=command,
            args=args,
            send_message=_send,
        )
        result = execute_skill(skill, ctx)
        if result:
            result_parts.append(str(result))
    except Exception as e:
        log.error("skill %s error: %s", command, e)
        return False, f"Error running skill: {e}"

    return True, "\n".join(result_parts).strip()


@bp.route("/v1/projects", methods=["GET"])
@require_token
def list_projects():
    from app.utils import get_known_projects
    projects = get_known_projects()
    result = []
    for name, path in projects:
        entry = {"name": name, "path": path, "github_url": None}
        try:
            from app.projects_config import load_projects_config, get_project_config
            cfg = load_projects_config(str(_koan_root()))
            if cfg:
                proj_cfg = get_project_config(cfg, name)
                github_url = proj_cfg.get("github_url", "")
                if github_url:
                    entry["github_url"] = github_url
        except Exception as e:
            log.warning("github_url lookup failed for project %s: %s", name, e)
        result.append(entry)
    return jsonify(result)


@bp.route("/v1/projects", methods=["POST"])
@openapi_operation(request_schema=_ADD_PROJECT_SCHEMA)
@require_token
def add_project():
    data = request.get_json(silent=True) or {}
    github_url = data.get("github_url", "").strip()
    if not github_url:
        return jsonify({"error": {"code": "invalid_request", "message": "'github_url' is required"}}), 422

    name = data.get("name", "").strip()
    args = github_url
    if name:
        args = f"{github_url} {name}"

    ok, result = _run_skill("add_project", args)
    if not ok:
        return jsonify({"error": {"code": "skill_error", "message": result}}), 500
    return jsonify({"result": result}), 201


@bp.route("/v1/projects/<name>", methods=["DELETE"])
@require_token
def delete_project(name: str):
    ok, result = _run_skill("delete_project", name)
    if not ok:
        return jsonify({"error": {"code": "skill_error", "message": result}}), 500
    return jsonify({"result": result})


@bp.route("/v1/projects/<name>", methods=["PATCH"])
@openapi_operation(request_schema=_PATCH_PROJECT_SCHEMA)
@require_token
def patch_project(name: str):
    from app.projects_config import apply_project_patch

    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get("patch"), dict):
        return jsonify({"error": {"code": "invalid_request", "message": "Missing 'patch' object"}}), 422

    try:
        merged = apply_project_patch(str(_koan_root()), name, data["patch"])
    except ValueError as exc:
        msg = str(exc)
        if msg.startswith("Unknown project"):
            return jsonify({"error": {"code": "not_found", "message": msg}}), 404
        return jsonify({"error": {"code": "invalid_request", "message": msg}}), 422

    return jsonify({"name": name, "config": merged}), 200
