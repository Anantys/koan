"""Kōan ci_recovery skill — show CI failure recovery status."""

import json
import os
from pathlib import Path


def handle(ctx):
    """Show CI recovery status for all tracked Kōan PRs.

    Lists all PRs with active CI recovery tracking, their attempt counts,
    and current status.
    """
    from app.check_tracker import _load

    instance_dir = ctx.instance_dir
    data = _load(instance_dir)

    tracked = []
    for url, entry in data.items():
        ci = entry.get("ci")
        if ci:
            tracked.append((url, ci))

    if not tracked:
        return "No active CI recovery sessions."

    lines = ["*CI Recovery Status*\n"]
    for url, ci in tracked:
        status = ci.get("status", "unknown")
        attempts = ci.get("attempt_count", 0)
        last_at = ci.get("last_attempt_at", "?")[:19]  # trim microseconds
        lines.append(
            f"• {url}\n"
            f"  Status: {status} | Attempts: {attempts} | Last: {last_at}"
        )

    return "\n".join(lines)
