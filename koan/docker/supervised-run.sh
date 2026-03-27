#!/bin/sh
# Thin wrapper for supervisord-managed processes.
# On first start: run immediately.
# On restart after crash: wait 10s (matches systemd RestartSec=10).
MARKER="/tmp/.koan-started-${SUPERVISOR_PROCESS_NAME}"
if [ -f "$MARKER" ]; then
    echo "[${SUPERVISOR_PROCESS_NAME}] crashed — restarting in 10s..."
    sleep 10
fi
touch "$MARKER"
exec "$@"
