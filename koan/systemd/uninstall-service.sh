#!/usr/bin/env bash
# Uninstall Kōan systemd services.
# Usage: ./uninstall-service.sh
set -euo pipefail

if [ "$(uname -s)" != "Linux" ]; then
    echo "Error: systemd services are only supported on Linux." >&2
    exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
    echo "Error: systemctl not found." >&2
    exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "Error: must be run as root (use sudo)." >&2
    exit 1
fi

SERVICES="koan.service koan-awake.service"

for svc in $SERVICES; do
    if [ -f "/etc/systemd/system/$svc" ]; then
        echo "→ Stopping $svc"
        systemctl stop "$svc" 2>/dev/null || true
        echo "→ Disabling $svc"
        systemctl disable "$svc" 2>/dev/null || true
        echo "→ Removing $svc"
        rm -f "/etc/systemd/system/$svc"
    else
        echo "  $svc not installed, skipping"
    fi
done

echo "→ Reloading systemd daemon"
systemctl daemon-reload

echo "✓ Kōan systemd services removed."
