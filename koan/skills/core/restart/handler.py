"""Handler for /restart command.

Restarts both agent and bridge processes without pulling new code.

``/restart`` is polite: the runner finishes the mission it is on before
exiting. ``/restart --force`` is not — it marks the request forced and sends
SIGUSR2 so the runner kills the in-flight mission and restarts immediately.
Use it when the run loop is wedged. The killed mission is re-queued by crash
recovery on the next startup.
"""

import signal as sig_mod

from app.skills import SkillContext

_FORCE_FLAGS = {"--force", "-f", "force"}


def handle(ctx: SkillContext) -> str:
    """Request a restart of both processes."""
    from app.pid_manager import signal_process
    from app.restart_manager import request_restart

    force = any(arg in _FORCE_FLAGS for arg in ctx.args.lower().split())
    request_restart(str(ctx.koan_root), force=force)

    if not force:
        return "🔄 Restart requested. Both processes will restart shortly."

    # Wake the runner immediately — without the signal it would only notice
    # the forced marker on its next 30 s mission poll (and not at all while
    # it is blocked elsewhere). signal_process verifies the PID still belongs
    # to run.py, since SIGUSR2's default disposition kills the target.
    signalled = signal_process(ctx.koan_root, "run", sig_mod.SIGUSR2)
    detail = "" if signalled else " (runner not running — it will start clean)"
    return (
        "🔄 Force restart requested. Any in-flight mission is killed now and "
        f"re-queued on startup{detail}."
    )
