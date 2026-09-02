"""Kōan abort skill -- abort the current in-progress mission.

Writes ``.koan-abort`` AND sends SIGUSR1 to the run process so the
abort takes effect within milliseconds. Without the signal, the runner
would only notice the file on its next ``proc.wait`` poll (up to 30 s).
The file remains as a durability fallback: if the signal is lost (runner
restarting, PID file stale), the poll loop still picks it up.
"""

import signal as sig_mod

from app.skills import SkillContext


def handle(ctx: SkillContext) -> str:
    """Handle /abort command."""
    from app.pid_manager import signal_process
    from app.signals import ABORT_FILE
    from app.utils import atomic_write

    abort_path = ctx.koan_root / ABORT_FILE
    atomic_write(abort_path, "abort")

    # Wake the runner immediately via SIGUSR1. The runner's handler kills
    # the active Claude subprocess and clears the abort file. If the runner
    # is paused / between missions, the signal is harmless (no claude_proc).
    # signal_process guards against a recycled PID belonging to an unrelated
    # process (SIGUSR1's default disposition would kill it).
    signal_process(ctx.koan_root, "run", sig_mod.SIGUSR1)

    return "⏭️ Abort requested. Current mission will be aborted and moved to Failed."
