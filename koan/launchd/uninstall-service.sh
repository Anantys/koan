#!/usr/bin/env bash
# Uninstall Kōan launchd services (macOS).
# Usage: ./uninstall-service.sh
set -euo pipefail

if [ "$(uname -s)" != "Darwin" ]; then
    echo "Error: launchd services are only supported on macOS." >&2
    exit 1
fi

if ! command -v launchctl >/dev/null 2>&1; then
    echo "Error: launchctl not found." >&2
    exit 1
fi

LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
GUI_DOMAIN="gui/$(id -u)"
SERVICES="com.koan.run com.koan.awake"

for plist in $SERVICES; do
    PLIST_PATH="$LAUNCH_AGENTS_DIR/$plist.plist"
    if [ -f "$PLIST_PATH" ]; then
        echo "→ Unloading $plist"
        launchctl bootout "$GUI_DOMAIN/$plist" 2>/dev/null || true
        echo "→ Removing $plist.plist"
        rm -f "$PLIST_PATH"
    else
        echo "  $plist.plist not installed, skipping"
    fi
done

echo "✓ Kōan launchd services removed."
