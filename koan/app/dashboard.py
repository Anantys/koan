#!/usr/bin/env python3
"""
Kōan — Local Dashboard

Flask web app for monitoring and interacting with Kōan.
Features:
- Status overview (signal files, run state)
- Missions management (view, add, reorder)
- Chat interface (writes to outbox, queues missions)
- Journal viewer
- Live progress (SSE stream of pending.md)

Usage:
    python3 dashboard.py [--port 5001]
    make dashboard
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, render_template, request, url_for
from app.cli_provider import build_full_command
from app.config import (
    get_allowed_tools,
    get_tools_description,
    get_model_config,
)
from app.conversation_history import (
    save_conversation_message,
    load_recent_history,
    format_conversation_history,
)
from app.signals import (
    DAILY_REPORT_FILE,
    FOCUS_FILE,
    PAUSE_FILE,
    PROJECT_FILE,
    QUOTA_RESET_FILE,
    STATUS_FILE,
    STOP_FILE,
)
from app.missions import (
    cancel_pending_mission,
    edit_pending_mission,
    extract_project_tag,
    group_by_project,
    reorder_mission,
)
from app.utils import (
    modify_missions_file,
    parse_project,
    insert_pending_mission,
    get_known_projects,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

KOAN_ROOT = Path(os.environ["KOAN_ROOT"])
INSTANCE_DIR = KOAN_ROOT / "instance"
MISSIONS_FILE = INSTANCE_DIR / "missions.md"
OUTBOX_FILE = INSTANCE_DIR / "outbox.md"
SOUL_FILE = INSTANCE_DIR / "soul.md"
SUMMARY_FILE = INSTANCE_DIR / "memory" / "summary.md"
JOURNAL_DIR = INSTANCE_DIR / "journal"
PENDING_FILE = JOURNAL_DIR / "pending.md"
CONVERSATION_HISTORY_FILE = INSTANCE_DIR / "conversation-history.jsonl"
CHAT_TIMEOUT = int(os.environ.get("KOAN_CHAT_TIMEOUT", "180"))

app = Flask(
    __name__,
    template_folder=str(KOAN_ROOT / "koan" / "templates"),
    static_folder=str(Path(__file__).parent.parent / "static"),
    static_url_path="/static",
)


_PROJECT_TAG_RE = re.compile(r'\s*\[(?:project|projet):([a-zA-Z0-9_-]+)\]\s*')


@app.template_filter('strip_project_tag')
def strip_project_tag_filter(text: str) -> str:
    """Remove [project:name] tag from mission text for display."""
    return _PROJECT_TAG_RE.sub(' ', text).strip()


@app.template_filter('project_badge')
def project_badge_filter(text: str) -> str:
    """Extract project tag and return badge HTML, or empty string."""
    m = _PROJECT_TAG_RE.search(text)
    if m:
        name = m.group(1)
        return f'<span class="badge badge-blue">{name}</span> '
    return ''


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def read_file(path: Path) -> str:
    if path.exists():
        return path.read_text()
    return ""


def get_signal_status() -> dict:
    """Read .koan-* signal files."""
    status = {
        "stop_requested": (KOAN_ROOT / STOP_FILE).exists(),
        "quota_paused": (KOAN_ROOT / QUOTA_RESET_FILE).exists(),
        "paused": (KOAN_ROOT / PAUSE_FILE).exists(),
        "loop_status": "",
        "pause_reason": "",
        "reset_time": "",
    }

    # Read pause reason from .koan-pause content
    if status["paused"]:
        from app.pause_manager import get_pause_state
        state = get_pause_state(str(KOAN_ROOT))
        if state:
            status["pause_reason"] = state.reason
            if state.display:
                status["reset_time"] = state.display
            elif state.timestamp:
                try:
                    from app.reset_parser import time_until_reset
                    status["reset_time"] = f"in ~{time_until_reset(state.timestamp)}"
                except (ValueError, ImportError):
                    pass

    status_file = KOAN_ROOT / STATUS_FILE
    if status_file.exists():
        status["loop_status"] = status_file.read_text().strip()
    report_file = KOAN_ROOT / DAILY_REPORT_FILE
    if report_file.exists():
        status["last_report"] = report_file.read_text().strip()
    return status


# Staleness threshold — if .koan-status mtime is older than this, treat as idle
_STALE_THRESHOLD_SECONDS = 300  # 5 minutes

# Patterns to classify .koan-status text into agent states.
# Order matters: first match wins.
_STATUS_PATTERNS = [
    # Error recovery
    (re.compile(r"Error recovery"), "error_recovery"),
    # Paused (written by run.py when quota-paused)
    (re.compile(r"Paused"), "paused"),
    # Contemplative (must be before Idle — text starts with "Idle —")
    (re.compile(r"post-contemplation"), "contemplating"),
    # Idle / sleeping
    (re.compile(r"Idle"), "sleeping"),
    # Executing / working states
    (re.compile(r"Run \d+/\d+ — executing"), "working"),
    (re.compile(r"Run \d+/\d+ — skill dispatch"), "working"),
    (re.compile(r"Run \d+/\d+ — (REVIEW|IMPLEMENT|DEEP)"), "working"),
    (re.compile(r"Run \d+/\d+ — preparing"), "working"),
    (re.compile(r"Run \d+/\d+ — finalizing"), "working"),
    (re.compile(r"Run \d+/\d+ — done"), "working"),
]

# Badge color per state
_BADGE_COLORS = {
    "working": "green",
    "sleeping": "blue",
    "contemplating": "blue",
    "paused": "orange",
    "stopped": "red",
    "error_recovery": "red",
    "idle": "muted",
}

# Extract "Run X/Y" from status text
_RUN_INFO_RE = re.compile(r"Run (\d+/\d+)")

# Extract autonomous mode from status text (e.g. "REVIEW on koan")
_MODE_RE = re.compile(r"— (REVIEW|IMPLEMENT|DEEP)\b")

# Extract project name from "on <project>" in status text
_STATUS_PROJECT_RE = re.compile(r"on (\S+)\s*$")


def get_agent_state() -> dict:
    """Derive a structured agent state from signal files.

    Returns a dict with keys: state, label, project, run_info, pause_reason,
    reset_time, focus, elapsed, badge_color.
    """
    signals = get_signal_status()
    status_text = signals.get("loop_status", "")

    # Read project from .koan-project
    project_file = KOAN_ROOT / PROJECT_FILE
    project = ""
    if project_file.exists():
        try:
            project = project_file.read_text().strip()
        except OSError:
            pass

    # Read focus state
    focus = None
    focus_file = KOAN_ROOT / FOCUS_FILE
    if focus_file.exists():
        try:
            from app.focus_manager import get_focus_state
            fs = get_focus_state(str(KOAN_ROOT))
            if fs and not fs.is_expired():
                focus = {
                    "remaining": fs.remaining_display(),
                    "reason": fs.reason,
                }
        except (OSError, ImportError):
            pass

    # Calculate elapsed time since status file was last written
    elapsed = 0
    status_file = KOAN_ROOT / STATUS_FILE
    is_stale = False
    if status_file.exists():
        try:
            elapsed = int(time.time() - status_file.stat().st_mtime)
            is_stale = elapsed > _STALE_THRESHOLD_SECONDS
        except OSError:
            pass

    # Determine state with priority: stopped > paused > status text > idle
    if signals["stop_requested"]:
        state = "stopped"
        label = "Stopped"
    elif signals["paused"] or signals["quota_paused"]:
        state = "paused"
        reason = signals.get("pause_reason", "")
        reset = signals.get("reset_time", "")
        # quota_paused flag (.koan-quota-reset) may exist without .koan-pause
        if signals["quota_paused"] and not reason:
            reason = "quota"
        if reason == "quota":
            label = f"Paused — quota{f' ({reset})' if reset else ''}"
        elif reason:
            label = f"Paused — {reason}"
        else:
            label = "Paused"
    elif status_text and not is_stale:
        # Classify from status text patterns
        state = "idle"
        for pattern, matched_state in _STATUS_PATTERNS:
            if pattern.search(status_text):
                state = matched_state
                break
        label = status_text
    else:
        state = "idle"
        label = "Idle" if not is_stale else "Idle (stale)"

    # Extract run_info from status text
    run_info = ""
    m = _RUN_INFO_RE.search(status_text)
    if m:
        run_info = m.group(1)

    # Extract autonomous mode
    autonomous_mode = ""
    m = _MODE_RE.search(status_text)
    if m:
        autonomous_mode = m.group(1)

    # Extract project from status text if not set from .koan-project
    if not project:
        m = _STATUS_PROJECT_RE.search(status_text)
        if m:
            project = m.group(1)

    return {
        "state": state,
        "label": label,
        "project": project,
        "run_info": run_info,
        "autonomous_mode": autonomous_mode,
        "pause_reason": signals.get("pause_reason", ""),
        "reset_time": signals.get("reset_time", ""),
        "focus": focus,
        "elapsed": elapsed,
        "badge_color": _BADGE_COLORS.get(state, "muted"),
    }


def parse_missions() -> dict:
    """Parse missions.md into structured sections."""
    from app.missions import parse_sections

    content = read_file(MISSIONS_FILE)
    if not content:
        return {"pending": [], "in_progress": [], "done": []}

    return parse_sections(content)


def _filter_missions_by_project(missions: dict, project: str) -> dict:
    """Filter parsed mission sections to only items matching project tag."""
    if not project:
        return missions
    return {
        key: [m for m in items if extract_project_tag(m) == project]
        for key, items in missions.items()
    }


def _get_all_project_names() -> list:
    """Return sorted list of project names from config and mission tags."""
    # Names from projects.yaml / env
    names = {name for name, _path in get_known_projects()}
    # Names from mission tags
    missions = parse_missions()
    for section in missions.values():
        for item in section:
            tag = extract_project_tag(item)
            if tag != "default":
                names.add(tag)
    return sorted(names, key=str.lower)


def get_journal_entries(limit: int = 7) -> list:
    """Get recent journal entries."""
    entries = []
    if not JOURNAL_DIR.exists():
        return entries

    # Collect all journal dates (both flat and nested)
    dates = set()
    for item in sorted(JOURNAL_DIR.iterdir(), reverse=True):
        if item.is_dir() and re.match(r"\d{4}-\d{2}-\d{2}", item.name):
            dates.add(item.name)
        elif item.suffix == ".md" and re.match(r"\d{4}-\d{2}-\d{2}", item.stem):
            dates.add(item.stem)

    for d in sorted(dates, reverse=True)[:limit]:
        day_entries = []
        # Check nested structure
        nested = JOURNAL_DIR / d
        if nested.is_dir():
            for f in sorted(nested.glob("*.md")):
                day_entries.append({
                    "project": f.stem,
                    "content": f.read_text(),
                })
        # Check flat structure
        flat = JOURNAL_DIR / f"{d}.md"
        if flat.is_file():
            day_entries.append({
                "project": "general",
                "content": flat.read_text(),
            })
        if day_entries:
            entries.append({"date": d, "entries": day_entries})

    return entries


def _build_dashboard_prompt(text: str, *, lite: bool = False) -> str:
    """Build the prompt for a dashboard chat response.

    Args:
        text: The user's message.
        lite: If True, strip heavy context (journal, summary) to reduce prompt size.
    """
    from app.journal import read_all_journals

    history = load_recent_history(CONVERSATION_HISTORY_FILE, max_messages=10)
    history_context = format_conversation_history(history)

    soul = read_file(SOUL_FILE)

    summary = ""
    if not lite:
        summary = read_file(SUMMARY_FILE)[:1500]

    journal_context = ""
    if not lite:
        journal_content = read_all_journals(INSTANCE_DIR, date.today())
        if journal_content:
            journal_context = journal_content[-2000:] if len(journal_content) > 2000 else journal_content

    from app.prompts import load_prompt

    tools_desc = get_tools_description()
    summary_block = f"Summary of past sessions:\n{summary}" if summary else ""
    journal_block = f"Today's journal (excerpt):\n{journal_context}" if journal_context else ""

    return load_prompt(
        "dashboard-chat",
        SOUL=soul,
        TOOLS_DESC=tools_desc or "",
        SUMMARY=summary_block,
        JOURNAL=journal_block,
        HISTORY=history_context or "",
        TEXT=text,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    """Main dashboard page."""
    agent_state = get_agent_state()
    selected_project = request.args.get("project", "")
    missions = parse_missions()
    filtered = _filter_missions_by_project(missions, selected_project)

    # Per-project stats for multi-project summary
    project_stats = {}
    projects_list = _get_all_project_names()
    if len(projects_list) > 1:
        by_project = group_by_project(read_file(MISSIONS_FILE))
        for pname, pdata in by_project.items():
            project_stats[pname] = {
                "pending": len(pdata["pending"]),
                "in_progress": len(pdata["in_progress"]),
            }

    # Map structured state to the template's existing state vocabulary
    tpl_state = agent_state["state"]
    if tpl_state in ("working", "contemplating", "error_recovery"):
        tpl_state = "running"
    elif tpl_state == "sleeping":
        tpl_state = "running"

    return render_template("dashboard.html",
        state=tpl_state,
        state_label=agent_state["label"],
        agent_state=agent_state,
        signals=get_signal_status(),
        missions=filtered,
        pending_count=len(filtered["pending"]),
        in_progress_count=len(filtered["in_progress"]),
        done_count=len(filtered["done"]),
        selected_project=selected_project,
        project_stats=project_stats,
    )


@app.route("/missions")
def missions_page():
    """Missions management page."""
    selected_project = request.args.get("project", "")
    missions = parse_missions()
    filtered = _filter_missions_by_project(missions, selected_project)
    projects = [name for name, _path in get_known_projects()]
    return render_template("missions.html", missions=filtered,
                           selected_project=selected_project, projects=projects)


@app.route("/missions/add", methods=["POST"])
def add_mission():
    """Add a new mission to pending."""
    from app.missions import sanitize_mission_text

    text = sanitize_mission_text(request.form.get("mission", ""))
    project = request.form.get("project", "").strip()
    if not text:
        return redirect(url_for("missions_page"))

    # Format entry
    if project:
        entry = f"- [project:{project}] {text}"
    else:
        entry = f"- {text}"

    insert_pending_mission(MISSIONS_FILE, entry)
    return redirect(url_for("missions_page"))


@app.route("/chat")
def chat_page():
    """Chat interface."""
    return render_template("chat.html")


@app.route("/chat/send", methods=["POST"])
def chat_send():
    """Send a message — either as mission or direct outbox message."""
    from app.cli_exec import run_cli

    text = request.form.get("message", "").strip()
    mode = request.form.get("mode", "chat")  # chat or mission

    if not text:
        return jsonify({"ok": False, "error": "Empty message"})

    if mode == "mission":
        # Queue as mission (same logic as awake.py)
        from app.missions import sanitize_mission_text

        text = sanitize_mission_text(text)
        project, mission_text = parse_project(text)
        if project:
            entry = f"- [project:{project}] {mission_text}"
        else:
            entry = f"- {mission_text}"

        insert_pending_mission(MISSIONS_FILE, entry)
        return jsonify({"ok": True, "type": "mission", "text": mission_text})

    else:
        # Direct chat — call claude CLI like awake.py does
        # Save user message to history
        save_conversation_message(CONVERSATION_HISTORY_FILE, "user", text)

        prompt = _build_dashboard_prompt(text)
        project_path = os.environ.get("KOAN_CURRENT_PROJECT_PATH", str(KOAN_ROOT))
        allowed_tools_list = get_allowed_tools().split(",")
        models = get_model_config()

        cmd = build_full_command(
            prompt=prompt,
            allowed_tools=allowed_tools_list,
            model=models["chat"],
            fallback=models["fallback"],
            max_turns=1,
        )

        try:
            result = run_cli(
                cmd,
                capture_output=True, text=True, timeout=CHAT_TIMEOUT,
                cwd=project_path,
            )
            response = result.stdout.strip()
            if result.returncode != 0:
                print(f"[dashboard] Claude error (exit {result.returncode}): {result.stderr[:200]}", file=sys.stderr)
            if not response:
                if result.stderr:
                    print(f"[dashboard] Claude stderr: {result.stderr[:500]}")
                response = "I couldn't formulate a response. Try again?"
            # Save assistant response to history
            save_conversation_message(CONVERSATION_HISTORY_FILE, "assistant", response)
            return jsonify({"ok": True, "type": "chat", "response": response})
        except subprocess.TimeoutExpired:
            # Retry with lite context (no journal, no summary) like awake.py
            print(f"[dashboard] Chat timed out ({CHAT_TIMEOUT}s). Retrying with lite context...")
            lite_prompt = _build_dashboard_prompt(text, lite=True)
            lite_cmd = build_full_command(
                prompt=lite_prompt,
                allowed_tools=allowed_tools_list,
                model=models["chat"],
                fallback=models["fallback"],
                max_turns=1,
            )
            try:
                result = run_cli(
                    lite_cmd,
                    capture_output=True, text=True, timeout=CHAT_TIMEOUT,
                    cwd=project_path,
                )
                if result.stderr:
                    print(f"[dashboard] Lite retry stderr: {result.stderr[:500]}")
                response = result.stdout.strip()
                if result.returncode != 0:
                    print(f"[dashboard] Claude error on retry (exit {result.returncode}): {result.stderr[:200]}", file=sys.stderr)
                if response:
                    save_conversation_message(CONVERSATION_HISTORY_FILE, "assistant", response)
                    return jsonify({"ok": True, "type": "chat", "response": response})
                else:
                    timeout_msg = f"Timeout after {CHAT_TIMEOUT}s — try a shorter question."
                    save_conversation_message(CONVERSATION_HISTORY_FILE, "assistant", timeout_msg)
                    return jsonify({"ok": True, "type": "chat", "response": timeout_msg})
            except subprocess.TimeoutExpired:
                timeout_msg = f"Timeout after {CHAT_TIMEOUT}s — try a shorter question."
                save_conversation_message(CONVERSATION_HISTORY_FILE, "assistant", timeout_msg)
                return jsonify({"ok": True, "type": "chat", "response": timeout_msg})
            except (OSError, ValueError) as e:
                return jsonify({"ok": False, "error": str(e)})
        except (OSError, ValueError) as e:
            return jsonify({"ok": False, "error": str(e)})


@app.route("/progress")
def progress_page():
    """Live progress page — tails pending.md via SSE."""
    return render_template("progress.html")


@app.route("/api/progress")
def api_progress():
    """JSON snapshot of pending.md content."""
    content = read_file(PENDING_FILE)
    return jsonify({
        "active": PENDING_FILE.exists(),
        "content": content,
    })


@app.route("/api/progress/stream")
def api_progress_stream():
    """SSE stream of pending.md changes.

    Polls the file every second, sends an event when content changes.
    Sends a heartbeat comment every 15s to keep the connection alive.
    """
    def generate():
        last_content = None
        last_mtime = 0.0
        heartbeat_counter = 0

        while True:
            try:
                if PENDING_FILE.exists():
                    st = PENDING_FILE.stat()
                    if st.st_mtime != last_mtime:
                        last_mtime = st.st_mtime
                        content = PENDING_FILE.read_text()
                        if content != last_content:
                            last_content = content
                            payload = json.dumps({
                                "active": True,
                                "content": content,
                            })
                            yield f"data: {payload}\n\n"
                            heartbeat_counter = 0
                else:
                    if last_content is not None:
                        # File was deleted — mission completed
                        payload = json.dumps({
                            "active": False,
                            "content": "",
                        })
                        yield f"data: {payload}\n\n"
                        last_content = None
                        last_mtime = 0.0
                        heartbeat_counter = 0
            except OSError:
                pass

            heartbeat_counter += 1
            if heartbeat_counter >= 15:
                yield ": heartbeat\n\n"
                heartbeat_counter = 0

            time.sleep(1)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/state/stream")
def api_state_stream():
    """SSE stream of agent state changes.

    Polls signal files every 2s, sends an event when state changes.
    Sends a heartbeat comment every 15s to keep the connection alive.
    Includes attention_count (cached at 30s TTL) in each payload.
    """
    def generate():
        last_json = None
        heartbeat_counter = 0
        # Mutable containers for mtime-based mission count caching
        missions_mtime = [0.0]
        missions_counts = [{"pending": 0, "in_progress": 0, "done": 0}]

        while True:
            try:
                state = get_agent_state()
                # Add attention count (cheap — uses 30s cache)
                try:
                    from app.attention import get_attention_count
                    state["attention_count"] = get_attention_count(str(KOAN_ROOT))
                except Exception as e:
                    print(f"[dashboard] attention count error: {e}", file=sys.stderr)
                    state["attention_count"] = 0
                # Add mission counts (uses mtime check to avoid re-parsing)
                try:
                    if MISSIONS_FILE.exists():
                        mtime = MISSIONS_FILE.stat().st_mtime
                        if mtime != missions_mtime[0]:
                            missions_mtime[0] = mtime
                            m = parse_missions()
                            missions_counts[0] = {
                                "pending": len(m["pending"]),
                                "in_progress": len(m["in_progress"]),
                                "done": len(m["done"]),
                            }
                    else:
                        missions_counts[0] = {"pending": 0, "in_progress": 0, "done": 0}
                except OSError:
                    pass
                state["missions"] = missions_counts[0]
                state_json = json.dumps(state, sort_keys=True)
                if state_json != last_json:
                    last_json = state_json
                    yield f"data: {json.dumps(state)}\n\n"
                    heartbeat_counter = 0
            except OSError:
                pass

            heartbeat_counter += 1
            if heartbeat_counter >= 8:  # 8 * 2s = 16s ~ 15s heartbeat
                yield ": heartbeat\n\n"
                heartbeat_counter = 0

            time.sleep(2)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/usage")
def usage_page():
    """Usage tracking page — per-project and per-model token breakdown."""
    return render_template("usage.html")


@app.route("/api/usage")
def api_usage():
    """JSON usage data for the specified time range."""
    from app.cost_tracker import (
        summarize_range,
        get_pricing_config,
        estimate_cost,
        estimate_cache_savings,
        daily_series,
    )

    days = request.args.get("days", "7", type=str)
    selected_project = request.args.get("project", "")
    try:
        days = int(days)
        days = max(1, min(days, 90))
    except (ValueError, TypeError):
        days = 7

    end = date.today()
    start = end - timedelta(days=days - 1)
    summary = summarize_range(INSTANCE_DIR, start, end)

    by_project = summary["by_project"]
    if selected_project and by_project:
        by_project = {k: v for k, v in by_project.items() if k == selected_project}

    pricing = get_pricing_config()

    # Compute aggregate estimated cost across all models
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
        estimated_cost = total_cost

    # Per-day time series for charts
    daily = daily_series(INSTANCE_DIR, start, end, project=selected_project or None)
    estimated_cache_savings = estimate_cache_savings(summary, pricing)

    return jsonify({
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
        "daily": daily,
    })


@app.route("/api/metrics")
def api_metrics():
    """JSON mission metrics for the specified time range."""
    from app.mission_metrics import (
        compute_global_metrics,
        compute_project_metrics,
        compute_project_trend,
    )

    days = request.args.get("days", "30", type=str)
    selected_project = request.args.get("project", "")
    try:
        days = int(days)
        days = max(0, min(days, 365))
    except (ValueError, TypeError):
        days = 30

    if selected_project:
        metrics = compute_project_metrics(str(INSTANCE_DIR), selected_project, days=days)
        metrics["trend"] = compute_project_trend(str(INSTANCE_DIR), selected_project, days=days)
        return jsonify(metrics)

    # Global metrics with per-project trends
    metrics = compute_global_metrics(str(INSTANCE_DIR), days=days)
    for proj in metrics["by_project"]:
        metrics["by_project"][proj]["trend"] = compute_project_trend(
            str(INSTANCE_DIR), proj, days=days
        )
    return jsonify(metrics)


@app.route("/journal")
def journal_page():
    """Journal viewer."""
    selected_project = request.args.get("project", "")
    entries = get_journal_entries(limit=14)
    if selected_project:
        filtered = []
        for day in entries:
            day_filtered = [e for e in day["entries"] if e["project"] == selected_project]
            if day_filtered:
                filtered.append({"date": day["date"], "entries": day_filtered})
        entries = filtered
    return render_template("journal.html", entries=entries, selected_project=selected_project)


@app.route("/api/projects")
def api_projects():
    """Return list of known project names."""
    return jsonify({"projects": _get_all_project_names()})


@app.route("/api/missions")
def api_missions():
    """Return full mission lists as JSON."""
    missions = parse_missions()
    return jsonify({
        "pending": missions["pending"],
        "in_progress": missions["in_progress"],
        "done": missions["done"],
    })


@app.route("/api/missions/reorder", methods=["POST"])
def api_missions_reorder():
    """Reorder a pending mission."""
    data = request.get_json(silent=True) or {}
    position = data.get("position")
    target = data.get("target")

    if position is None or target is None:
        return jsonify({"ok": False, "error": "Missing position or target"}), 400

    try:
        position = int(position)
        target = int(target)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "position and target must be integers"}), 400

    try:
        result = {}

        def transform(content):
            new_content, display = reorder_mission(content, position, target)
            result["display"] = display
            return new_content

        modify_missions_file(MISSIONS_FILE, transform)
        missions = parse_missions()
        return jsonify({
            "ok": True,
            "display": result.get("display", ""),
            "pending": missions["pending"],
        })
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/missions/cancel", methods=["POST"])
def api_missions_cancel():
    """Cancel a pending mission by position."""
    data = request.get_json(silent=True) or {}
    position = data.get("position")

    if position is None:
        return jsonify({"ok": False, "error": "Missing position"}), 400

    try:
        result = {}

        def transform(content):
            new_content, cancelled = cancel_pending_mission(content, str(int(position)))
            result["cancelled"] = cancelled
            return new_content

        modify_missions_file(MISSIONS_FILE, transform)
        missions = parse_missions()
        return jsonify({
            "ok": True,
            "cancelled": result.get("cancelled", ""),
            "pending": missions["pending"],
        })
    except (ValueError, TypeError) as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/missions/edit", methods=["POST"])
def api_missions_edit():
    """Edit a pending mission's text."""
    data = request.get_json(silent=True) or {}
    position = data.get("position")
    text = data.get("text", "").strip()

    if position is None:
        return jsonify({"ok": False, "error": "Missing position"}), 400
    if not text:
        return jsonify({"ok": False, "error": "Mission text cannot be empty"}), 400

    try:
        position = int(position)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "position must be an integer"}), 400

    try:
        result = {}

        def transform(content):
            new_content, display = edit_pending_mission(content, position, text)
            result["display"] = display
            return new_content

        modify_missions_file(MISSIONS_FILE, transform)
        missions = parse_missions()
        return jsonify({
            "ok": True,
            "display": result.get("display", ""),
            "pending": missions["pending"],
        })
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/attention")
def api_attention():
    """JSON list of attention items requiring human action."""
    from app.attention import get_attention_items

    project = request.args.get("project", "")
    items = get_attention_items(str(KOAN_ROOT), project_filter=project)
    return jsonify({"items": items})


@app.route("/api/attention/dismiss", methods=["POST"])
def api_attention_dismiss():
    """Dismiss an attention item by ID."""
    from app.attention import dismiss_item

    data = request.get_json(silent=True) or {}
    item_id = data.get("id", "").strip()
    if not item_id:
        return jsonify({"ok": False, "error": "Missing id"}), 400
    dismiss_item(str(KOAN_ROOT), item_id)
    return jsonify({"ok": True})


@app.route("/prs")
def prs_page():
    """PR tracking page — open PRs across all projects."""
    return render_template("prs.html")


@app.route("/api/prs")
def api_prs():
    """JSON endpoint for open PRs across projects."""
    from app.pr_tracker import fetch_all_prs

    project = request.args.get("project", "")
    author_only = request.args.get("author_only", "true").lower() != "false"
    data = fetch_all_prs(str(KOAN_ROOT), project_filter=project,
                         author_only=author_only)
    return jsonify(data)


@app.route("/api/prs/<project>/<int:number>/checks")
def api_pr_checks(project, number):
    """Fetch CI checks for a specific PR."""
    from app.pr_tracker import fetch_pr_checks

    checks = fetch_pr_checks(project, number, str(KOAN_ROOT))
    return jsonify({"checks": checks})


@app.route("/api/prs/<project>/<int:number>/merge", methods=["POST"])
def api_pr_merge(project, number):
    """Merge a PR (requires auto-merge enabled for the project)."""
    from app.pr_tracker import merge_pr

    result = merge_pr(project, number, str(KOAN_ROOT))
    status_code = 200 if result["ok"] else 400
    return jsonify(result), status_code


# ---------------------------------------------------------------------------
# Plans — GitHub-backed plan issue viewer
# ---------------------------------------------------------------------------

# In-memory cache: {cache_key: (timestamp, data)}
_plans_cache: dict = {}
_PLANS_CACHE_TTL = 60  # seconds


def _parse_plan_progress(markdown: str) -> dict:
    """Extract phase list and completion status from plan markdown.

    Plans follow a strict format with ``#### Phase N: Title`` headings.
    Completion is detected by ✅, [x]/[X], or "Done" markers in phase content.

    Returns a dict with keys:
        phases: list of {"title": str, "completed": bool}
        completed: int
        total: int
        percent: int
    """
    if not markdown:
        return {"phases": [], "completed": 0, "total": 0, "percent": 0}

    # Split markdown into lines for phase-aware parsing
    lines = markdown.splitlines()
    phases = []
    current_phase = None
    current_lines: list = []

    _phase_heading = re.compile(r'^####\s+Phase\s+\d+[:\s](.+)', re.IGNORECASE)
    # "Done" matches as completion only when NOT followed by "when" (avoids "Done when:" field)
    _done_marker = re.compile(r'✅|\[x\]|\bDone\b(?!\s+when)', re.IGNORECASE)

    def _finalize_phase(phase, content_lines):
        content = '\n'.join(content_lines)
        completed = bool(_done_marker.search(content))
        phases.append({"title": phase, "completed": completed})

    for line in lines:
        m = _phase_heading.match(line)
        if m:
            if current_phase is not None:
                _finalize_phase(current_phase, current_lines)
            current_phase = m.group(1).strip()
            current_lines = []
        elif current_phase is not None:
            current_lines.append(line)

    if current_phase is not None:
        _finalize_phase(current_phase, current_lines)

    completed = sum(1 for p in phases if p["completed"])
    total = len(phases)
    percent = int(completed / total * 100) if total else 0
    return {"phases": phases, "completed": completed, "total": total, "percent": percent}


def _get_project_repo(project_name: str) -> str | None:
    """Return owner/repo string for a project, or None if not available."""
    from app.projects_config import get_project_config
    from app.github_url_parser import parse_github_url

    config = get_project_config(str(KOAN_ROOT), project_name)
    github_url = config.get("github_url", "")
    if not github_url:
        return None
    try:
        owner, repo, _, _ = parse_github_url(github_url + "/issues/1")
        return f"{owner}/{repo}"
    except ValueError:
        # github_url may already be owner/repo or just a base URL
        # Try parsing as base URL: https://github.com/owner/repo
        m = re.match(r'https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$', github_url)
        if m:
            return f"{m.group(1)}/{m.group(2)}"
    return None


def _fetch_plans_for_project(project_name: str, repo: str) -> list:
    """Fetch open plan issues for a project via gh CLI."""
    from app.github import run_gh

    try:
        raw = run_gh(
            "search", "issues",
            "--repo", repo,
            "--label", "plan",
            "--state", "open",
            "--json", "number,title,state,body,updatedAt,url",
            "--limit", "50",
            timeout=30,
        )
        issues = json.loads(raw) if raw else []
    except (RuntimeError, json.JSONDecodeError, OSError):
        return []

    result = []
    for issue in issues:
        body = issue.get("body") or ""
        progress = _parse_plan_progress(body)
        result.append({
            "number": issue.get("number"),
            "title": issue.get("title", ""),
            "state": issue.get("state", "open"),
            "url": issue.get("url", ""),
            "updatedAt": issue.get("updatedAt", ""),
            "body": body,
            "progress": progress,
            "project": project_name,
            "repo": repo,
        })
    return result


@app.route("/plans")
def plans_page():
    """Plans viewer page — plan issues across all projects."""
    return render_template("plans.html")


@app.route("/api/plans")
def api_plans():
    """JSON endpoint returning plan issues across all projects."""
    from app.utils import get_known_projects

    project_filter = request.args.get("project", "")
    force_refresh = request.args.get("force", "") == "1"
    now = time.time()
    all_plans = []
    errors = []

    known = get_known_projects()
    for project_name, _path in known:
        if project_filter and project_name != project_filter:
            continue

        cache_key = f"plans:{project_name}"
        if not force_refresh and cache_key in _plans_cache:
            cached_ts, cached_data = _plans_cache[cache_key]
            if now - cached_ts < _PLANS_CACHE_TTL:
                all_plans.extend(cached_data)
                continue

        repo = _get_project_repo(project_name)
        if not repo:
            continue

        plans = _fetch_plans_for_project(project_name, repo)
        _plans_cache[cache_key] = (now, plans)
        all_plans.extend(plans)

    # Sort by updatedAt descending
    all_plans.sort(key=lambda p: p.get("updatedAt", ""), reverse=True)

    return jsonify({"plans": all_plans, "errors": errors})


@app.route("/api/plans/<project>/<int:number>")
def api_plan_detail(project, number):
    """Single plan detail — full body + latest iteration (last comment)."""
    from app.github import run_gh

    repo = _get_project_repo(project)
    if not repo:
        return jsonify({"error": f"No github_url configured for project {project!r}"}), 404

    # Fetch issue with all comments
    try:
        raw = run_gh(
            "issue", "view", str(number),
            "--repo", repo,
            "--json", "number,title,state,body,url,updatedAt,comments",
            timeout=30,
        )
        issue = json.loads(raw) if raw else {}
    except (RuntimeError, json.JSONDecodeError, OSError) as e:
        return jsonify({"error": str(e)}), 502

    body = issue.get("body") or ""
    comments = issue.get("comments") or []

    # Latest iteration: last comment body if exists, else issue body
    latest_body = comments[-1].get("body", body) if comments else body

    # Linked missions: search missions.md for the issue URL
    issue_url = issue.get("url", "")
    linked_missions = _find_linked_missions(issue_url, number)

    progress = _parse_plan_progress(latest_body)

    return jsonify({
        "number": issue.get("number"),
        "title": issue.get("title", ""),
        "state": issue.get("state", "open"),
        "url": issue_url,
        "updatedAt": issue.get("updatedAt", ""),
        "body": body,
        "latest_body": latest_body,
        "comments": [{"body": c.get("body", ""), "createdAt": c.get("createdAt", "")} for c in comments],
        "progress": progress,
        "project": project,
        "repo": repo,
        "linked_missions": linked_missions,
    })


def _find_linked_missions(issue_url: str, issue_number: int) -> list:
    """Find missions that reference the given plan issue URL or number."""
    content = read_file(MISSIONS_FILE)
    if not content:
        return []

    linked = []
    issue_number_str = f"#{issue_number}"
    for line in content.splitlines():
        stripped = line.strip().lstrip("- ~")
        if issue_url and issue_url in line:
            linked.append(stripped)
        elif issue_number_str in line and "/plan" in line.lower():
            linked.append(stripped)
    return linked[:20]  # cap to avoid huge responses


@app.route("/api/status")
def api_status():
    """JSON status endpoint."""
    signals = get_signal_status()
    missions = parse_missions()
    return jsonify({
        "signals": signals,
        "missions": {
            "pending": len(missions["pending"]),
            "in_progress": len(missions["in_progress"]),
            "done": len(missions["done"]),
        },
        "agent_state": get_agent_state(),
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Kōan Dashboard")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--debug", action="store_true",
                        help="Enable Flask debug mode (NOT recommended)")
    args = parser.parse_args()

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"[dashboard] WARNING: Binding to {args.host} exposes the dashboard "
            f"to the network. No authentication or rate limiting is configured.",
            file=sys.stderr,
        )

    print(f"[dashboard] Starting on http://{args.host}:{args.port}")
    print(f"[dashboard] Instance: {INSTANCE_DIR}")
    app.run(host=args.host, port=args.port, debug=args.debug)
