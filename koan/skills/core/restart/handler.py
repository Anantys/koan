"""Handler for /restart command.

Restarts both agent and bridge processes without pulling new code.
"""

from app.skills import SkillContext


def handle(ctx: SkillContext) -> str:
    """Request a restart of both processes."""
    from app.restart_manager import request_restart

    request_restart(str(ctx.koan_root))
    return "🔄 Restart requested. Both processes will restart shortly."
