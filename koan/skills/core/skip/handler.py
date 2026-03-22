"""Kōan skip skill -- abort the current in-progress mission.

Writes a signal file that the agent loop detects during its 30-second
poll cycle. The running Claude subprocess is killed, the mission is
moved to Failed, and the loop continues with the next pending item.
"""

from app.skills import SkillContext


def handle(ctx: SkillContext) -> str:
    """Handle /skip command."""
    from app.signals import SKIP_FILE
    from app.utils import atomic_write

    skip_path = ctx.koan_root / SKIP_FILE
    atomic_write(skip_path, "skip")
    return "⏭️ Skip requested. Current mission will be aborted and moved to Failed."
