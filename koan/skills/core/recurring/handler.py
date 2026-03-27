"""Kōan recurring skill -- manage recurring missions (hourly, daily, weekly, every)."""


def handle(ctx):
    """Handle /daily, /hourly, /weekly, /every, /recurring, /cancel_recurring commands.

    /daily <text>           — add a daily recurring mission
    /hourly <text>          — add an hourly recurring mission
    /weekly <text>          — add a weekly recurring mission
    /every <interval> <text> — add a custom-interval recurring mission
    /recurring              — list all recurring missions
    /cancel_recurring [n]   — cancel a recurring mission by number or keyword
    """
    command = ctx.command_name

    if command in ("daily", "hourly", "weekly"):
        return _handle_add(ctx, command)
    elif command == "every":
        return _handle_every(ctx)
    elif command == "recurring":
        return _handle_list(ctx)
    elif command == "cancel_recurring":
        return _handle_cancel(ctx)

    return None


def _handle_add(ctx, frequency):
    """Add a recurring mission with the given frequency."""
    body = ctx.args.strip()
    if not body:
        return (
            f"Usage: /{frequency} [HH:MM] <description>\n"
            f"Ex: /{frequency} check open pull requests\n"
            f"Ex: /{frequency} 20:00 run nightly audit [project:myapp]"
        )

    from app.utils import parse_project
    from app.recurring import add_recurring, parse_at_time

    project, text = parse_project(body)

    try:
        at_time, text = parse_at_time(text)
    except ValueError as e:
        return str(e)

    recurring_path = ctx.instance_dir / "recurring.json"

    try:
        add_recurring(recurring_path, frequency, text, project, at=at_time)
        ack = f"Recurring mission added ({frequency}"
        if at_time:
            ack += f" at {at_time}"
        ack += ")"
        if project:
            ack += f" [project:{project}]"
        ack += f":\n\n{text}"
        return ack
    except ValueError as e:
        return str(e)


def _handle_every(ctx):
    """Add a recurring mission with a custom interval."""
    body = ctx.args.strip()
    if not body:
        return (
            "Usage: /every <interval> <description>\n"
            "Ex: /every 5m check design issues [project:nocrm]\n"
            "Ex: /every 2h run health check\n"
            "Intervals: 5m, 30m, 2h, 1h30m"
        )

    # First word is the interval
    parts = body.split(None, 1)
    if len(parts) < 2:
        return (
            "Usage: /every <interval> <description>\n"
            "Ex: /every 5m check design issues"
        )

    interval_str, rest = parts[0], parts[1]

    from app.utils import parse_project
    from app.recurring import parse_interval, format_interval, add_recurring_interval

    try:
        interval_seconds = parse_interval(interval_str)
    except ValueError as e:
        return str(e)

    project, text = parse_project(rest)
    if not text.strip():
        return "Missing mission description after interval."

    recurring_path = ctx.instance_dir / "recurring.json"
    display = format_interval(interval_seconds)

    add_recurring_interval(recurring_path, interval_seconds, display, text, project)
    ack = f"Recurring mission added (every {display})"
    if project:
        ack += f" [project:{project}]"
    ack += f":\n\n{text}"
    return ack


def _handle_list(ctx):
    """List all recurring missions."""
    from app.recurring import list_recurring, format_recurring_list

    recurring_path = ctx.instance_dir / "recurring.json"
    missions = list_recurring(recurring_path)
    return format_recurring_list(missions)


def _handle_cancel(ctx):
    """Cancel a recurring mission by number or keyword."""
    from app.recurring import list_recurring, format_recurring_list, remove_recurring

    recurring_path = ctx.instance_dir / "recurring.json"
    identifier = ctx.args.strip()

    if not identifier:
        missions = list_recurring(recurring_path)
        if missions:
            msg = format_recurring_list(missions)
            msg += "\n\nUsage: /cancel_recurring <number or keyword>"
            return msg
        return "No recurring missions to cancel."

    try:
        removed = remove_recurring(recurring_path, identifier)
        return f"Recurring mission removed: {removed}"
    except ValueError as e:
        return str(e)
