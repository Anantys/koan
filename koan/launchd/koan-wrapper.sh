#!/usr/bin/env bash
# Wrapper script for launchd-managed Kōan processes.
# Sources .env, sets up the environment, then execs the Python script.
#
# Usage: koan-wrapper.sh <script>
#   e.g.: koan-wrapper.sh app/run.py
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
KOAN_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

export KOAN_ROOT
export PYTHONPATH="$KOAN_ROOT/koan"

# Source .env if it exists (provides TELEGRAM_TOKEN, API keys, etc.)
if [ -f "$KOAN_ROOT/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$KOAN_ROOT/.env"
    set +a
fi

# Forward SSH agent socket if available
if [ -S "$KOAN_ROOT/.ssh-agent-sock" ]; then
    export SSH_AUTH_SOCK="$KOAN_ROOT/.ssh-agent-sock"
fi

PYTHON="$KOAN_ROOT/.venv/bin/python3"
if [ ! -x "$PYTHON" ]; then
    echo "Error: Python venv not found at $PYTHON — run 'make setup' first." >&2
    exit 1
fi

SCRIPT="${1:?Usage: $0 <script>}"
exec "$PYTHON" "$SCRIPT"
