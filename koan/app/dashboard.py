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
        "stop_requested": (KOAN_ROOT / ".koan-stop").exists(),
        "quota_paused": (KOAN_ROOT / ".koan-quota-reset").exists(),
        "paused": (KOAN_ROOT / ".koan-pause").exists(),
        "loop_status": "",
        "pause_reason": "",
        "reset_time": "",
    }

    # Read pause reason file for detailed status
    pause_reason_file = KOAN_ROOT / ".koan-pause-reason"
    if pause_reason_file.exists():
        try:
            lines = pause_reason_file.read_text().strip().split("\n")
            status["pause_reason"] = lines[0] if lines else ""
            if len(lines) > 2:
                status["reset_time"] = lines[2]  # Human-readable reset info
            elif len(lines) > 1:
                # Try to format the timestamp
                try:
                    from app.reset_parser import time_until_reset
                    ts = int(lines[1])
                    status["reset_time"] = f"in ~{time_until_reset(ts)}"
                except (ValueError, ImportError):
                    pass
        except OSError:
            pass

    status_file = KOAN_ROOT / ".koan-status"
    if status_file.exists():
        status["loop_status"] = status_file.read_text().strip()
    report_file = KOAN_ROOT / ".koan-daily-report"
    if report_file.exists():
        status["last_report"] = report_file.read_text().strip()
    return status


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
    signals = get_signal_status()
    missions = parse_missions()

    # Determine overall state
    if signals["stop_requested"]:
        state = "stopped"
        state_label = "Stopped"
    elif signals["quota_paused"]:
        state = "paused"
        state_label = "Quota Exhausted"
    elif signals["loop_status"]:
        state = "running"
        state_label = f"Run {signals['loop_status']}"
    else:
        state = "idle"
        state_label = "Idle"

    return render_template("dashboard.html",
        state=state,
        state_label=state_label,
        signals=signals,
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
