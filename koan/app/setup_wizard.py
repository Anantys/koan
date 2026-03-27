#!/usr/bin/env python3
"""
Kōan — Setup Wizard

Modern web-based installation wizard that guides users through:
1. Welcome + Claude Code verification
2. Telegram bot configuration
3. Project paths setup
4. Final verification + launch

Usage:
    python3 setup_wizard.py [--port 5002]
    make install
"""

import os
import re
import shutil
import subprocess
import webbrowser
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, redirect, render_template, request, url_for

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent
KOAN_ROOT = SCRIPT_DIR.parent.parent  # koan/app/.. → koan/.. → repo root
INSTANCE_DIR = KOAN_ROOT / "instance"
INSTANCE_EXAMPLE = KOAN_ROOT / "instance.example"
ENV_FILE = KOAN_ROOT / ".env"
ENV_EXAMPLE = KOAN_ROOT / "env.example"
CONFIG_FILE = INSTANCE_DIR / "config.yaml"

app = Flask(__name__, template_folder=str(KOAN_ROOT / "koan" / "templates"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_installation_status() -> dict:
    """Check what's already configured."""
    status = {
        "instance_exists": INSTANCE_DIR.exists(),
        "env_exists": ENV_FILE.exists(),
        "venv_exists": (KOAN_ROOT / ".venv").exists(),
        "claude_installed": shutil.which("claude") is not None,
        "telegram_configured": False,
        "projects_configured": False,
    }

    if ENV_FILE.exists():
        env_content = ENV_FILE.read_text()
        status["telegram_configured"] = (
            "KOAN_TELEGRAM_TOKEN=" in env_content
            and "your-bot-token" not in env_content
            and "KOAN_TELEGRAM_CHAT_ID=" in env_content
            and "your-chat-id" not in env_content
        )
        status["projects_configured"] = (
            (KOAN_ROOT / "projects.yaml").exists()
            or (
                "KOAN_PROJECTS=" in env_content
                and "/path/to" not in env_content
            )
        )

    return status


def _load_wizard_projects() -> list:
    """Load project list for wizard display.

    Reuses get_known_projects() (projects.yaml > KOAN_PROJECTS).
    Returns list of dicts with 'name' and 'path' keys.
    """
    from app.utils import get_known_projects

    try:
        return [{"name": name, "path": path} for name, path in get_known_projects()]
    except (OSError, ValueError, ImportError):
        return []


def create_instance_dir() -> bool:
    """Copy instance.example to instance if it doesn't exist."""
    if INSTANCE_DIR.exists():
        return True
    if not INSTANCE_EXAMPLE.exists():
        return False
    shutil.copytree(INSTANCE_EXAMPLE, INSTANCE_DIR)
    return True


def create_env_file() -> bool:
    """Copy env.example to .env if it doesn't exist."""
    if ENV_FILE.exists():
        return True
    if not ENV_EXAMPLE.exists():
        return False
    shutil.copy(ENV_EXAMPLE, ENV_FILE)
    return True


def update_env_var(key: str, value: str) -> bool:
    """Update or add an environment variable in .env file."""
    if not ENV_FILE.exists():
        return False

    content = ENV_FILE.read_text()
    lines = content.split("\n")
    updated = False
    new_lines = []

    for line in lines:
        if line.startswith(f"{key}=") or line.startswith(f"# {key}="):
            new_lines.append(f"{key}={value}")
            updated = True
        else:
            new_lines.append(line)

    if not updated:
        new_lines.append(f"{key}={value}")

    ENV_FILE.write_text("\n".join(new_lines))
    return True


def get_env_var(key: str) -> Optional[str]:
    """Read an environment variable from .env file."""
    if not ENV_FILE.exists():
        return None

    content = ENV_FILE.read_text()
    for line in content.split("\n"):
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def verify_telegram_token(token: str) -> dict:
    """Verify a Telegram bot token by calling getMe."""
    import urllib.request
    import json

    try:
        url = f"https://api.telegram.org/bot{token}/getMe"
        with urllib.request.urlopen(url, timeout=10) as response:
            data = json.loads(response.read().decode())
            if data.get("ok"):
                bot_info = data.get("result", {})
                return {
                    "valid": True,
                    "username": bot_info.get("username", ""),
                    "first_name": bot_info.get("first_name", ""),
                }
    except Exception as e:
        return {"valid": False, "error": str(e)}

    return {"valid": False, "error": "Invalid token"}


def get_chat_id_from_updates(token: str) -> Optional[str]:
    """Try to get chat ID from recent updates."""
    import urllib.request
    import json

    try:
        url = f"https://api.telegram.org/bot{token}/getUpdates?limit=5"
        with urllib.request.urlopen(url, timeout=10) as response:
            data = json.loads(response.read().decode())
            if data.get("ok"):
                updates = data.get("result", [])
                for update in updates:
                    msg = update.get("message", {})
                    chat = msg.get("chat", {})
                    if chat.get("id"):
                        return str(chat["id"])
    except (OSError, ValueError):
        pass
    return None


def run_make_setup() -> tuple[bool, str]:
    """Run make setup to create venv and install dependencies."""
    try:
        result = subprocess.run(
            ["make", "setup"],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(KOAN_ROOT),
        )
        success = result.returncode == 0
        output = result.stdout + result.stderr
        return success, output
    except subprocess.TimeoutExpired:
        return False, "Setup timed out after 5 minutes"
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    """Redirect to appropriate step based on installation status."""
    status = get_installation_status()

    # Determine current step
    if not status["claude_installed"]:
        return redirect(url_for("step_welcome"))
    if not status["instance_exists"] or not status["env_exists"]:
        return redirect(url_for("step_welcome"))
    if not status["telegram_configured"]:
        return redirect(url_for("step_telegram"))
    if not status["projects_configured"]:
        return redirect(url_for("step_projects"))

    return redirect(url_for("step_ready"))


@app.route("/step/welcome")
def step_welcome():
    """Step 1: Welcome + prerequisites check."""
    status = get_installation_status()
    hostname_hint = not os.environ.get("SETUP_HOSTNAME")
    return render_template("wizard/welcome.html",
        status=status,
        koan_root=str(KOAN_ROOT),
        hostname_hint=hostname_hint,
    )


@app.route("/step/welcome/init", methods=["POST"])
def init_instance():
    """Initialize instance and env files."""
    create_instance_dir()
    create_env_file()
    update_env_var("KOAN_ROOT", str(KOAN_ROOT))
    return jsonify({"ok": True})


@app.route("/step/telegram")
def step_telegram():
    """Step 2: Telegram bot configuration."""
    status = get_installation_status()
    current_token = get_env_var("KOAN_TELEGRAM_TOKEN") or ""
    current_chat_id = get_env_var("KOAN_TELEGRAM_CHAT_ID") or ""

    # Mask token for display
    if current_token and "your-bot-token" not in current_token:
        display_token = current_token[:10] + "..." + current_token[-10:]
    else:
        display_token = ""

    return render_template("wizard/telegram.html",
        status=status,
        display_token=display_token,
        current_chat_id=current_chat_id if "your-chat-id" not in current_chat_id else "",
    )


@app.route("/step/telegram/verify", methods=["POST"])
def verify_telegram():
    """Verify Telegram token and optionally fetch chat ID."""
    data = request.get_json()
    token = data.get("token", "").strip()

    if not token:
        return jsonify({"ok": False, "error": "Token is required"})

    result = verify_telegram_token(token)
    if result["valid"]:
        # Try to get chat ID from recent messages
        chat_id = get_chat_id_from_updates(token)
        result["chat_id"] = chat_id

    return jsonify(result)


@app.route("/step/telegram/save", methods=["POST"])
def save_telegram():
    """Save Telegram configuration."""
    data = request.get_json()
    token = data.get("token", "").strip()
    chat_id = data.get("chat_id", "").strip()

    if not token or not chat_id:
        return jsonify({"ok": False, "error": "Token and Chat ID are required"})

    update_env_var("KOAN_TELEGRAM_TOKEN", token)
    update_env_var("KOAN_TELEGRAM_CHAT_ID", chat_id)

    return jsonify({"ok": True})


@app.route("/step/projects")
def step_projects():
    """Step 3: Project paths configuration."""
    status = get_installation_status()

    projects = _load_wizard_projects()

    return render_template("wizard/projects.html",
        status=status,
        projects=projects,
    )


@app.route("/step/projects/validate", methods=["POST"])
def validate_project():
    """Validate a project path."""
    data = request.get_json()
    path = data.get("path", "").strip()

    if not path:
        return jsonify({"valid": False, "error": "Path is required"})

    project_path = Path(path).expanduser()

    if not project_path.exists():
        return jsonify({"valid": False, "error": "Path does not exist"})

    if not project_path.is_dir():
        return jsonify({"valid": False, "error": "Path is not a directory"})

    # Check writability
    if not os.access(project_path, os.W_OK):
        return jsonify({"valid": False, "error": "Path is not writable"})

    # Check for CLAUDE.md (optional but nice to have)
    has_claude_md = (project_path / "CLAUDE.md").exists()

    # Check for .git
    is_git_repo = (project_path / ".git").exists()

    return jsonify({
        "valid": True,
        "has_claude_md": has_claude_md,
        "is_git_repo": is_git_repo,
        "absolute_path": str(project_path),
    })


@app.route("/step/projects/save", methods=["POST"])
def save_projects():
    """Save project configuration to projects.yaml."""
    data = request.get_json()
    projects = data.get("projects", [])

    if not projects:
        return jsonify({"ok": False, "error": "At least one project is required"})

    # Validate all projects exist
    for p in projects:
        path = Path(p.get("path", "")).expanduser()
        if not path.exists() or not path.is_dir():
            return jsonify({"ok": False, "error": f"Invalid path: {p.get('path')}"})

    # Build projects.yaml content
    import yaml

    config = {
        "defaults": {
            "git_auto_merge": {
                "enabled": False,
                "base_branch": "main",
                "strategy": "squash",
            }
        },
        "projects": {},
    }
    for p in sorted(projects, key=lambda x: x.get("name", "").lower()):
        name = p.get("name", Path(p["path"]).name)
        config["projects"][name] = {"path": str(Path(p["path"]).expanduser())}

    projects_yaml = KOAN_ROOT / "projects.yaml"
    header = (
        "# projects.yaml — Project configuration for Kōan\n"
        "#\n"
        "# See projects.example.yaml for full documentation.\n\n"
    )
    projects_yaml.write_text(
        header + yaml.dump(config, default_flow_style=False, sort_keys=False)
    )

    return jsonify({"ok": True})


@app.route("/step/ready")
def step_ready():
    """Step 4: Ready to launch!"""
    status = get_installation_status()

    projects = _load_wizard_projects()

    return render_template("wizard/ready.html",
        status=status,
        projects=projects,
    )


@app.route("/step/ready/setup", methods=["POST"])
def run_setup():
    """Run make setup to install dependencies."""
    success, output = run_make_setup()
    return jsonify({"ok": success, "output": output})


@app.route("/step/ready/finish", methods=["POST"])
def finish_setup():
    """Mark setup as complete and provide launch instructions."""
    status = get_installation_status()

    all_good = (
        status["instance_exists"]
        and status["env_exists"]
        and status["telegram_configured"]
        and status["projects_configured"]
    )

    return jsonify({
        "ok": all_good,
        "status": status,
        "launch_commands": {
            "terminal1": "make awake  # Telegram bridge",
            "terminal2": "make run    # Agent loop",
        },
    })


@app.route("/api/status")
def api_status():
    """Get current installation status."""
    return jsonify(get_installation_status())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Kōan Setup Wizard")
    parser.add_argument("--port", type=int, default=5002)
    parser.add_argument("--host", default=os.environ.get("SETUP_HOSTNAME", "127.0.0.1"))
    parser.add_argument("--no-browser", action="store_true", help="Don't open browser automatically")
    args = parser.parse_args()

    url = f"http://{args.host}:{args.port}"

    print(f"""
╔══════════════════════════════════════════════════════════════════╗
║                                                                  ║
║   ██╗  ██╗ ██████╗  █████╗ ███╗   ██╗                          ║
║   ██║ ██╔╝██╔═══██╗██╔══██╗████╗  ██║                          ║
║   █████╔╝ ██║   ██║███████║██╔██╗ ██║                          ║
║   ██╔═██╗ ██║   ██║██╔══██║██║╚██╗██║                          ║
║   ██║  ██╗╚██████╔╝██║  ██║██║ ╚████║                          ║
║   ╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═══╝                          ║
║                                                                  ║
║   Setup Wizard                                                   ║
║                                                                  ║
╚══════════════════════════════════════════════════════════════════╝

    Starting setup wizard at: {url}
{f"""
    💡 To bind to a different address, restart with:
       SETUP_HOSTNAME=<your-ip> make install
""" if args.host == "127.0.0.1" else ""}
    Press Ctrl+C to stop.
""")

    if not args.no_browser:
        # Open browser after a short delay
        import threading
        def open_browser():
            import time
            time.sleep(1.5)
            webbrowser.open(url)
        threading.Thread(target=open_browser, daemon=True).start()

    # Silence Flask's default output for cleaner UX
    import logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)

    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
