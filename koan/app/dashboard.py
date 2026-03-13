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
from app.utils import (
    parse_project,
    insert_pending_mission,
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

app = Flask(__name__, template_folder=str(KOAN_ROOT / "koan" / "templates"))


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
    # Idle / sleeping
    (re.compile(r"Idle"), "sleeping"),
    # Executing / working states
    (re.compile(r"Run \d+/\d+ — executing"), "working"),
    (re.compile(r"Run \d+/\d+ — skill dispatch"), "working"),
    (re.compile(r"Run \d+/\d+ — (REVIEW|IMPLEMENT|DEEP)"), "working"),
    (re.compile(r"Run \d+/\d+ — preparing"), "working"),
    (re.compile(r"Run \d+/\d+ — finalizing"), "working"),
    (re.compile(r"Run \d+/\d+ — done"), "working"),
    # Contemplative
    (re.compile(r"post-contemplation"), "contemplating"),
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
    missions = parse_missions()

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
        missions=missions,
        pending_count=len(missions["pending"]),
        in_progress_count=len(missions["in_progress"]),
        done_count=len(missions["done"]),
    )


@app.route("/missions")
def missions_page():
    """Missions management page."""
    missions = parse_missions()
    return render_template("missions.html", missions=missions)


@app.route("/missions/add", methods=["POST"])
def add_mission():
    """Add a new mission to pending."""
    text = request.form.get("mission", "").strip()
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
    """
    def generate():
        last_json = None
        heartbeat_counter = 0

        while True:
            try:
                state = get_agent_state()
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
    from app.cost_tracker import summarize_range, get_pricing_config

    days = request.args.get("days", "7", type=str)
    try:
        days = int(days)
        days = max(1, min(days, 90))
    except (ValueError, TypeError):
        days = 7

    end = date.today()
    start = end - timedelta(days=days - 1)
    summary = summarize_range(INSTANCE_DIR, start, end)

    pricing = get_pricing_config()
    return jsonify({
        "days": days,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "total_input": summary["total_input"],
        "total_output": summary["total_output"],
        "count": summary["count"],
        "by_project": summary["by_project"],
        "by_model": summary["by_model"],
        "has_pricing": pricing is not None,
    })


@app.route("/journal")
def journal_page():
    """Journal viewer."""
    entries = get_journal_entries(limit=14)
    return render_template("journal.html", entries=entries)


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
