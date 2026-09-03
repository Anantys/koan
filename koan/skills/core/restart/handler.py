"""Handler for /restart command.

Restarts both agent and bridge processes without pulling new code.

``/restart`` is polite: the runner finishes the mission it is on before
exiting. ``/restart --force`` is not — it marks the request forced and sends
SIGUSR2 so the runner kills the in-flight mission and restarts immediately.
Use it when the run loop is wedged. The killed mission is re-queued by crash
recovery on the next startup, unless it has already used up its crash retries
(``max_crash_retries``) — recovery then escalates it to Failed.

SIGUSR2 is only sent to a runner that advertises the ``sigusr2`` capability
(``.koan-run-caps``). A runner from a pre-upgrade image has no handler for it,
so the signal's default disposition would terminate it outright — leaving its
provider subprocess orphaned in its own session. Without the capability the
forced restart degrades to the polite one, and the reply says so.
"""

import signal as sig_mod

from app.skills import SkillContext

_FORCE_FLAGS = {"--force", "-f", "force"}

_FORCED = (
    "🔄 Force restart requested. Any in-flight mission is killed now and "
    "re-queued on startup — unless it has exhausted its crash retries, in "
    "which case recovery moves it to Failed."
)

_NO_RUNNER = (
    "🔄 Force restart requested. The agent loop is not running — it will "
    "start clean."
)

_NOT_CAPABLE = (
    "🔄 Force restart requested, but the running agent loop predates forced "
    "restart — it has no SIGUSR2 handler, so signalling it would kill it "
    "outright and orphan its provider session. Falling back to a polite "
    "restart: it exits once the current mission finishes."
)

_SIGNAL_LOST = (
    "🔄 Force restart requested. Could not signal the agent loop — falling "
    "back to the forced-marker poll (up to 30 s), which kills the in-flight "
    "mission the same way."
)


def handle(ctx: SkillContext) -> str:
    """Request a restart of both processes."""
    from app.restart_manager import request_restart

    force = any(arg in _FORCE_FLAGS for arg in ctx.args.lower().split())
    request_restart(str(ctx.koan_root), force=force)

    if not force:
        return "🔄 Restart requested. Both processes will restart shortly."
    return _force_restart_runner(ctx)


def _force_restart_runner(ctx: SkillContext) -> str:
    """Wake the runner for a forced restart; describe what actually happened.

    Without the signal the runner would only notice the forced marker on its
    next mission poll (and not at all while it is blocked elsewhere) — but
    signalling is gated on the runner advertising a SIGUSR2 handler, since
    the signal kills a runner that lacks one.
    """
    from app.pid_manager import check_pidfile, signal_process
    from app.restart_manager import runner_supports_force_signal
    from app.run_log import log

    pid = check_pidfile(ctx.koan_root, "run")
    if not pid:
        return _NO_RUNNER

    if not runner_supports_force_signal(ctx.koan_root, pid):
        log(
            "warning",
            f"Force restart: runner PID {pid} does not advertise SIGUSR2 "
            "— degrading to a polite restart",
        )
        return _NOT_CAPABLE

    # signal_process re-verifies the PID still belongs to run.py before
    # signalling, so a recycled PID is never hit.
    if not signal_process(ctx.koan_root, "run", sig_mod.SIGUSR2):
        log(
            "warning",
            "Force restart: SIGUSR2 not delivered to the runner — falling "
            "back to the forced restart marker",
        )
        return _SIGNAL_LOST

    return _FORCED
