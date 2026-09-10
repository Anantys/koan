"""REST API status routes."""

import logging
from pathlib import Path

from flask import Blueprint, current_app, jsonify

from app.api.auth import require_token
from app.api.openapi_metadata import openapi_operation

log = logging.getLogger("koan.api")

bp = Blueprint("status", __name__)


def _instance_dir() -> Path:
    return current_app.config["INSTANCE_DIR"]


def _koan_root() -> Path:
    return current_app.config["KOAN_ROOT"]


def _get_agent_state() -> dict:
    """Derive structured agent state from signal files.

    Delegates to ``agent_state`` module and reshapes the result into the
    REST API's response contract.
    """
    from app.agent_state import get_agent_state

    full = get_agent_state(_koan_root())

    pause_info: dict = {}
    if full["state"] == "paused":
        from app.pause_manager import get_pause_state

        ps = get_pause_state(str(_koan_root()))
        if ps:
            pause_info = {
                "reason": ps.reason,
                "timestamp": ps.timestamp,
                "display": ps.display,
            }

    return {
        "state": full["state"],
        "mode": full["autonomous_mode"] or None,
        "run_info": full["run_info"] or None,
        "project": full["project"],
        "focus": full["focus"] is not None,
        "status_text": full["label"],
        "pause": pause_info,
        "elapsed_seconds": full["elapsed"],
        "execution": full.get("execution"),
    }


def _mission_counts() -> dict:
    """Count missions by state from the authoritative store.

    Uses the same sync path as ``transition.read_sections`` (``ensure_store_synced``
    + a direct store read) so the counts agree with every other read surface.
    """
    instance = _instance_dir()
    try:
        from app.mission_store import get_mission_store
        from app.mission_store.transition import ensure_store_synced
        ensure_store_synced(str(instance))
        c = get_mission_store(str(instance)).counts()
    except Exception as e:
        # A misconfigured/broken store must be distinguishable from a healthy
        # empty queue — surface an explicit error rather than reporting zeros as
        # if valid. Keep the count keys (0) so downstream consumers don't break.
        log.error("mission count error: %s", e)
        return {"pending": 0, "in_progress": 0, "done": 0, "failed": 0,
                "error": str(e)}
    return {
        "pending": c.get("pending", 0),
        "in_progress": c.get("in_progress", 0),
        "done": c.get("done", 0),
        "failed": c.get("failed", 0),
    }


def _signal_flags() -> dict:
    """Boolean flags for stop/quota/pause signal files."""
    from app.agent_state import get_signal_status

    sigs = get_signal_status(_koan_root())
    return {
        "stop_requested": sigs["stop_requested"],
        "quota_paused": sigs["quota_paused"],
        "paused": sigs["paused"],
    }


def _attention_count() -> int:
    """Count unresolved attention items, fallback to 0 on error."""
    from app.attention import get_attention_count

    try:
        return get_attention_count(str(_koan_root()))
    except Exception as e:
        log.warning("attention count unavailable: %s", e)
        return 0


def _execution_truth(agent: dict, missions: dict, koan_root: Path) -> dict:
    """Reconcile declarative In Progress state against observed liveness (#2086).

    A mission line in *In Progress* with no live provider process is a zombie:
    surfaced loudly here rather than silently reported as "running". The
    zombie determination is shared with ``make status`` via
    ``active_mission.is_zombie`` (debounced against the normal start/stop
    windows and aware of parallel sessions).
    """
    from app.active_mission import is_zombie

    execution = agent.get("execution") or {}
    exec_state = execution.get("state", "idle")
    in_progress = missions.get("in_progress", 0) > 0
    zombie = is_zombie(koan_root, in_progress=in_progress, execution=execution)
    return {
        "provider_state": exec_state,
        "in_progress_lines": missions.get("in_progress", 0),
        "zombie": zombie,
    }


@bp.route("/v1/status")
@openapi_operation(mcp=True)
@require_token
def status():
    """Get current agent state, execution and mission counters."""
    agent = _get_agent_state()
    missions = _mission_counts()
    return jsonify(
        {
            "agent": agent,
            "missions": missions,
            "signals": _signal_flags(),
            "attention_count": _attention_count(),
            "execution": _execution_truth(agent, missions, _koan_root()),
        }
    )
