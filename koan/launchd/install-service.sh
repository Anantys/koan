#!/usr/bin/env bash
# Install Kōan launchd services (macOS).
# Usage: ./install-service.sh <koan_root>
set -euo pipefail

KOAN_ROOT="${1:?Usage: $0 <koan_root>}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# --- Validations ---

if [ "$(uname -s)" != "Darwin" ]; then
    echo "Error: launchd services are only supported on macOS." >&2
    exit 1
fi

if ! command -v launchctl >/dev/null 2>&1; then
    echo "Error: launchctl not found." >&2
    exit 1
fi

# Resolve to absolute path
KOAN_ROOT="$(cd "$KOAN_ROOT" && pwd)"

if [ ! -f "$KOAN_ROOT/koan/app/run.py" ]; then
    echo "Error: $KOAN_ROOT does not look like a Kōan installation." >&2
    exit 1
fi

PYTHON="$KOAN_ROOT/.venv/bin/python3"
if [ ! -x "$PYTHON" ]; then
    echo "Error: Python venv not found at $PYTHON — run 'make setup' first." >&2
    exit 1
fi

# --- Generate plist files via Python ---

mkdir -p "$KOAN_ROOT/logs"

LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
mkdir -p "$LAUNCH_AGENTS_DIR"

PYTHONPATH="$KOAN_ROOT/koan" "$PYTHON" -m app.launchd_service "$KOAN_ROOT" "$LAUNCH_AGENTS_DIR"

# --- Load services ---

GUI_DOMAIN="gui/$(id -u)"

for plist in com.koan.awake com.koan.run; do
    PLIST_PATH="$LAUNCH_AGENTS_DIR/$plist.plist"
    if [ ! -f "$PLIST_PATH" ]; then
        echo "Error: $PLIST_PATH not found after generation." >&2
        exit 1
    fi

    # Unload first if already loaded (idempotent)
    launchctl bootout "$GUI_DOMAIN/$plist" 2>/dev/null || true

    echo "→ Loading $plist"
    launchctl bootstrap "$GUI_DOMAIN" "$PLIST_PATH"
done

echo "✓ Kōan launchd services installed and loaded."
echo "  Services will auto-start at login."
echo "  Use 'make stop' to stop, 'make start' to start."
