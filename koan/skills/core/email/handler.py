"""Koan email skill — check status, send test emails."""


def handle(ctx):
    """Handle /email command — status or test."""
    from app.email_notify import can_send_email, get_email_stats, send_owner_email

    args = ctx.args.strip()

    if not args or args == "status":
        stats = get_email_stats()
        if not stats["enabled"]:
            return "Email: disabled in config.yaml"
        allowed, reason = can_send_email()
        parts = ["Email Status"]
        parts.append(f"  Enabled: {'yes' if stats['enabled'] else 'no'}")
        parts.append(f"  Sent (24h): {stats['sent_today']}/{stats['max_per_day']}")
        parts.append(f"  Remaining: {stats['remaining']}")
        if stats["last_sent"]:
            from datetime import datetime
            ts = datetime.fromtimestamp(stats["last_sent"]).strftime("%H:%M")
            parts.append(f"  Last sent: {ts}")
        if not allowed:
            parts.append(f"  Warning: {reason}")
        return "\n".join(parts)

    if args == "test":
        ok = send_owner_email(
            "Test email",
            "This is a test email from Koan. If you receive this, email is working.",
            skip_duplicate_check=True,
        )
        if ok:
            return "Test email sent."
        _, reason = can_send_email()
        return f"Test email failed: {reason}"

    return (
        "/email commands:\n"
        "  /email -- show email status\n"
        "  /email test -- send a test email"
    )
