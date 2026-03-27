"""Handler for /checkup skill — PR health check across all projects."""

import os


def handle(ctx):
    """Run PR checkup and return summary."""
    from app.pr_checkup import run_checkup

    instance_dir = str(ctx.instance_dir)
    koan_root = os.environ.get("KOAN_ROOT", "")

    if not koan_root:
        return "KOAN_ROOT not set — cannot run checkup"

    success, summary = run_checkup(
        koan_root=koan_root,
        instance_dir=instance_dir,
        notify_fn=ctx.reply,
    )

    # Reply was already sent via notify_fn during execution
    return ""
