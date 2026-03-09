"""
Kōan diagnostic — Process health checks.

Checks PID files for run/awake/ollama, pause state, heartbeat
freshness, and disk space.
"""

import shutil
from pathlib import Path
from typing import List

from diagnostics import CheckResult


def run(koan_root: str, instance_dir: str) -> List[CheckResult]:
    """Run process health diagnostic checks."""
    results = []
    root = Path(koan_root)

    # --- PID files (run, awake) ---
    from app.pid_manager import check_pidfile

    for process_name in ("run", "awake"):
        pid = check_pidfile(root, process_name)
        if pid:
            results.append(CheckResult(
                name=f"process_{process_name}",
                severity="ok",
                message=f"{process_name} is running (PID {pid})",
            ))
        else:
            # Check for stale PID file
            pid_file = root / f".koan-pid-{process_name}"
            if pid_file.exists():
                results.append(CheckResult(
                    name=f"process_{process_name}",
                    severity="warn",
                    message=f"Stale PID file for {process_name} (process not alive)",
                    hint=f"Remove {pid_file} or run 'make start'",
                ))
            else:
                results.append(CheckResult(
                    name=f"process_{process_name}",
                    severity="warn",
                    message=f"{process_name} is not running",
                    hint=f"Run 'make start' to launch all processes",
                ))

    # --- Ollama (only if provider needs it) ---
    try:
        from app.provider import get_provider_name
        provider = get_provider_name()
        if provider in ("local", "ollama"):
            pid = check_pidfile(root, "ollama")
            if pid:
                results.append(CheckResult(
                    name="process_ollama",
                    severity="ok",
                    message=f"ollama is running (PID {pid})",
                ))
            else:
                results.append(CheckResult(
                    name="process_ollama",
                    severity="warn",
                    message="ollama is not running (required by provider)",
                    hint="Run 'ollama serve &' or 'make ollama'",
                ))
    except Exception:
        pass  # Provider detection failed — skip ollama check

    # --- Pause state ---
    from app.pause_manager import get_pause_state, is_paused

    if is_paused(koan_root):
        state = get_pause_state(koan_root)
        if state:
            results.append(CheckResult(
                name="pause_state",
                severity="warn",
                message=f"Agent is paused (reason: {state.reason})",
                hint="/resume to unpause" if state.reason == "manual" else "Will auto-resume when quota resets",
            ))
        else:
            results.append(CheckResult(
                name="pause_state",
                severity="warn",
                message="Agent is paused (unknown reason)",
                hint="/resume to unpause",
            ))
    else:
        results.append(CheckResult(
            name="pause_state",
            severity="ok",
            message="Agent is not paused",
        ))

    # --- Heartbeat ---
    from app.health_check import check_heartbeat

    heartbeat_file = root / ".koan-heartbeat"
    if heartbeat_file.exists():
        if check_heartbeat(koan_root):
            results.append(CheckResult(
                name="heartbeat",
                severity="ok",
                message="Bridge heartbeat is fresh",
            ))
        else:
            results.append(CheckResult(
                name="heartbeat",
                severity="warn",
                message="Bridge heartbeat is stale",
                hint="Telegram bridge may be down — check 'make logs'",
            ))
    else:
        results.append(CheckResult(
            name="heartbeat",
            severity="ok",
            message="No heartbeat file (bridge not started yet)",
        ))

    # --- Disk space ---
    try:
        usage = shutil.disk_usage(koan_root)
        free_gb = usage.free / (1024 ** 3)
        if free_gb < 1.0:
            results.append(CheckResult(
                name="disk_space",
                severity="warn",
                message=f"Low disk space: {free_gb:.1f} GB free",
                hint="Free up disk space to avoid issues",
            ))
        else:
            results.append(CheckResult(
                name="disk_space",
                severity="ok",
                message=f"Disk space: {free_gb:.1f} GB free",
            ))
    except OSError as e:
        results.append(CheckResult(
            name="disk_space",
            severity="warn",
            message=f"Could not check disk space: {e}",
        ))

    return results
