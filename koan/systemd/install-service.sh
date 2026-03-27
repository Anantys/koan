#!/usr/bin/env bash
# Install Kōan systemd services.
# Usage: ./install-service.sh <koan_root> <python_path>
set -euo pipefail

KOAN_ROOT="${1:?Usage: $0 <koan_root> <python_path>}"
PYTHON="${2:?Usage: $0 <koan_root> <python_path>}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# --- Validations ---

if [ "$(uname -s)" != "Linux" ]; then
    echo "Error: systemd services are only supported on Linux." >&2
    exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
    echo "Error: systemctl not found. systemd is required." >&2
    exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "Error: must be run as root (use sudo)." >&2
    exit 1
fi

# Resolve to absolute paths
KOAN_ROOT="$(cd "$KOAN_ROOT" && pwd)"
PYTHON="$(cd "$(dirname "$PYTHON")" && pwd)/$(basename "$PYTHON")"

if [ ! -f "$KOAN_ROOT/koan/app/run.py" ]; then
    echo "Error: $KOAN_ROOT does not look like a Kōan installation." >&2
    exit 1
fi

if [ ! -x "$PYTHON" ]; then
    echo "Error: Python binary not found or not executable: $PYTHON" >&2
    exit 1
fi

# --- Generate service files via Python (testable PATH sanitization) ---

# Ensure the runtime user owns the logs directory
RUN_USER="${SUDO_USER:-$(whoami)}"
mkdir -p "$KOAN_ROOT/logs"
chown "$RUN_USER" "$KOAN_ROOT/logs"

CALLER_PATH="${CALLER_PATH:-$PATH}"
OUTPUT_DIR="/etc/systemd/system"
PYTHONPATH="$KOAN_ROOT/koan" "$PYTHON" -m app.systemd_service "$KOAN_ROOT" "$PYTHON" "$CALLER_PATH" "$OUTPUT_DIR"

# --- Enable and reload ---

echo "→ Reloading systemd daemon"
systemctl daemon-reload

echo "→ Enabling koan-awake.service"
systemctl enable koan-awake.service

echo "→ Enabling koan.service"
systemctl enable koan.service

echo "✓ Kōan systemd services installed and enabled."
echo "  Use 'make start' to start, or: systemctl start koan"
