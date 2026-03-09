"""Kōan /doctor skill — diagnostic self-checks."""

# Severity display mapping
_SEVERITY_ICONS = {
    "ok": "✅",
    "warn": "⚠️",
    "error": "❌",
}

# Module display names
_MODULE_NAMES = {
    "config_check": "Configuration",
    "environment_check": "Environment",
    "instance_check": "Instance",
    "process_check": "Processes",
    "project_check": "Projects",
    "connectivity_check": "Connectivity",
}

# Telegram message char limit (with margin)
_MAX_MESSAGE_LEN = 4000


def handle(ctx):
    """Run all diagnostic checks and format results."""
    from diagnostics import run_all

    koan_root = str(ctx.koan_root)
    instance_dir = str(ctx.instance_dir)
    args = (ctx.args or "").strip()
    full = "--full" in args

    all_results = run_all(koan_root, instance_dir, full=full)

    # Count totals
    ok_count = 0
    warn_count = 0
    error_count = 0

    for _module, checks in all_results:
        for check in checks:
            if check.severity == "ok":
                ok_count += 1
            elif check.severity == "warn":
                warn_count += 1
            elif check.severity == "error":
                error_count += 1

    # Build summary line
    total = ok_count + warn_count + error_count
    parts = []
    if ok_count:
        parts.append(f"{ok_count} passed")
    if warn_count:
        parts.append(f"{warn_count} warning(s)")
    if error_count:
        parts.append(f"{error_count} error(s)")
    summary = f"🩺 Doctor — {total} checks: {', '.join(parts)}"

    # Build sections
    sections = [summary, ""]
    for module_name, checks in all_results:
        display_name = _MODULE_NAMES.get(module_name, module_name)
        section_lines = [f"▸ {display_name}"]

        for check in checks:
            icon = _SEVERITY_ICONS.get(check.severity, "?")
            line = f"  {icon} {check.message}"
            section_lines.append(line)
            if check.hint and check.severity != "ok":
                section_lines.append(f"     ↳ {check.hint}")

        sections.append("\n".join(section_lines))

    if not full:
        sections.append("Run /doctor --full for connectivity checks")

    output = "\n\n".join(sections)

    # Handle message length limit — split if needed
    if len(output) <= _MAX_MESSAGE_LEN:
        return output

    # Split at section boundaries
    messages = []
    current = summary + "\n"

    for section in sections[1:]:
        if len(current) + len(section) + 2 > _MAX_MESSAGE_LEN:
            if current.strip():
                messages.append(current.strip())
            current = section + "\n\n"
        else:
            current += "\n" + section + "\n"

    if current.strip():
        messages.append(current.strip())

    # Send all but last via send_message, return last
    if hasattr(ctx, "send_message") and len(messages) > 1:
        for msg in messages[:-1]:
            ctx.send_message(msg)
        return messages[-1]

    return output
