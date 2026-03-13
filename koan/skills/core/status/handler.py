"""Kōan status skill — consolidates /status, /ping, /usage."""


def _needs_ollama() -> bool:
    """Return True if the configured provider requires ollama serve."""
    try:
        from app.provider import get_provider_name
        return get_provider_name() in ("local", "ollama")
    except Exception:
        return False


def _truncate(text: str, max_len: int = 60) -> str:
    """Truncate text with ellipsis."""
    if len(text) <= max_len:
        return text
    return text[:max_len - 1].rstrip() + "…"


def _format_mission_display(mission: str) -> str:
    """Format a mission for display: strip tags, add timing, truncate.

    Returns a clean, truncated mission string with optional timing info.
    """
    from app.missions import mission_timing_display, strip_timestamps
    from app.utils import parse_project

    # Remove project tags
    _, display = parse_project(mission)

    # Extract timing before stripping timestamps
    timing = mission_timing_display(display)

    # Clean up timestamps for display
    display = strip_timestamps(display)

    # Reserve space for timing suffix when truncating
    if timing:
        suffix = f" ({timing})"
        max_text = max(20, 60 - len(suffix))
        display = _truncate(display, max_text)
        display = f"{display}{suffix}"
    else:
        display = _truncate(display)

    return display


def handle(ctx):
    """Dispatch to the appropriate subcommand."""
    cmd = ctx.command_name
    if cmd == "ping":
        return _handle_ping(ctx)
    elif cmd == "usage":
        return _handle_usage(ctx)
    elif cmd == "metrics":
        return _handle_metrics(ctx)
    else:
        return _handle_status(ctx)


def _handle_status(ctx) -> str:
    """Build status message grouped by project."""
    from app.missions import group_by_project

    koan_root = ctx.koan_root
    instance_dir = ctx.instance_dir
    missions_file = instance_dir / "missions.md"

    parts = ["Kōan Status"]

    pause_file = koan_root / ".koan-pause"
    stop_file = koan_root / ".koan-stop"

    if stop_file.exists():
        parts.append("\n⛔ Mode: Stopping")
    elif pause_file.exists():
        from app.pause_manager import get_pause_state
        state = get_pause_state(str(koan_root))
        reason = state.reason if state else ""
        if reason == "quota":
            parts.append("\n⏸️ Mode: Paused (quota exhausted)")
        elif reason == "max_runs":
            parts.append("\n⏸️ Mode: Paused (max runs reached)")
        else:
            parts.append("\n⏸️ Mode: Paused")
        parts.append("  /resume to unpause")
    else:
        parts.append("\n🟢 Mode: Working")

    # Show focus mode if active
    try:
        from app.focus_manager import check_focus
        focus_state = check_focus(str(koan_root))
        if focus_state:
            parts.append(f"  🎯 Focus: missions only ({focus_state.remaining_display()} remaining)")
    except Exception:
        pass

    # Show process health when ollama is needed
    if _needs_ollama():
        from app.pid_manager import check_pidfile
        ollama_pid = check_pidfile(koan_root, "ollama")
        if ollama_pid:
            parts.append(f"  🦙 Ollama: running (PID {ollama_pid})")
        else:
            parts.append(f"  🦙 Ollama: not running")

    status_file = koan_root / ".koan-status"
    if status_file.exists():
        loop_status = status_file.read_text().strip()
        if loop_status:
            parts.append(f"  Loop: {loop_status}")

    # Show cache stats if cache has been used
    try:
        from app.response_cache import get_format_cache
        cache_stats = get_format_cache().stats()
        total = cache_stats["hits"] + cache_stats["misses"]
        if total > 0:
            parts.append(
                f"  Cache: {cache_stats['hits']} hits / {cache_stats['misses']} misses"
            )
    except Exception:
        pass

    if missions_file.exists():
        content = missions_file.read_text()
        missions_by_project = group_by_project(content)

        if missions_by_project:
            for project in sorted(missions_by_project.keys()):
                missions = missions_by_project[project]
                pending = missions["pending"]
                in_progress = missions["in_progress"]

                if pending or in_progress:
                    parts.append(f"\n{project}")
                    if in_progress:
                        parts.append(f"  In progress: {len(in_progress)}")
                        for m in in_progress[:2]:
                            parts.append(f"    {_format_mission_display(m)}")
                    if pending:
                        parts.append(f"  Pending: {len(pending)}")
                        for m in pending[:3]:
                            parts.append(f"    {_format_mission_display(m)}")

    # Health section
    parts.extend(_build_health_section(koan_root, instance_dir))

    return "\n".join(parts)


def _build_health_section(koan_root, instance_dir) -> list:
    """Build health status lines for /status output."""
    lines = []
    try:
        from app.health_check import get_run_heartbeat_age
        from app.heartbeat import check_stale_missions, get_disk_free_gb

        health_items = []

        # Run heartbeat age
        age = get_run_heartbeat_age(str(koan_root))
        if age >= 0:
            if age < 120:
                health_items.append(f"Heartbeat: {age:.0f}s ago")
            else:
                health_items.append(f"⚠️ Heartbeat: {age / 60:.0f}m ago")
        else:
            health_items.append("Heartbeat: n/a")

        # Stale missions (read-only check, no alerting)
        stale = check_stale_missions(str(instance_dir))
        if stale:
            health_items.append(f"⚠️ {len(stale)} stale mission(s)")

        # Disk space
        free_gb = get_disk_free_gb(str(koan_root))
        if free_gb >= 0:
            if free_gb < 1.0:
                health_items.append(f"⚠️ Disk: {free_gb:.1f} GB free")
            else:
                health_items.append(f"Disk: {free_gb:.0f} GB free")

        if health_items:
            lines.append("\nHealth")
            for item in health_items:
                lines.append(f"  {item}")
    except Exception:
        pass
    return lines


def _handle_ping(ctx) -> str:
    """Check if run and awake processes are alive using PID files."""
    from app.pid_manager import check_pidfile

    koan_root = ctx.koan_root
    run_pid = check_pidfile(koan_root, "run")
    awake_pid = check_pidfile(koan_root, "awake")

    pause_file = koan_root / ".koan-pause"
    stop_file = koan_root / ".koan-stop"

    lines = []

    # --- Runner status ---
    if run_pid:
        if stop_file.exists():
            lines.append(f"⏹️ Runner: stopping (PID {run_pid})")
        elif pause_file.exists():
            lines.append(f"⏸️ Runner: paused (PID {run_pid})")
            lines.append("  /resume to unpause")
        else:
            status_file = koan_root / ".koan-status"
            loop_status = ""
            if status_file.exists():
                loop_status = status_file.read_text().strip()
            if loop_status:
                lines.append(f"✅ Runner: {loop_status} (PID {run_pid})")
            else:
                lines.append(f"✅ Runner: alive (PID {run_pid})")
    else:
        lines.append("❌ Runner: not running")
        lines.append("  make run &")

    # --- Bridge status ---
    if awake_pid:
        lines.append(f"✅ Bridge: alive (PID {awake_pid})")
    else:
        lines.append("❌ Bridge: not running")
        lines.append("  make awake &")

    # --- Ollama status (only for local/ollama providers) ---
    if _needs_ollama():
        ollama_pid = check_pidfile(koan_root, "ollama")
        if ollama_pid:
            lines.append(f"✅ Ollama: alive (PID {ollama_pid})")
        else:
            lines.append("❌ Ollama: not running")
            lines.append("  ollama serve &")

    return "\n".join(lines)


def _handle_usage(ctx) -> str:
    """Build usage status. Returns raw data for the caller to format."""
    instance_dir = ctx.instance_dir
    missions_file = instance_dir / "missions.md"

    usage_text = "No quota data available."
    usage_path = instance_dir / "usage.md"
    if usage_path.exists():
        usage_text = usage_path.read_text().strip() or usage_text

    missions_text = "No missions."
    if missions_file.exists():
        from app.missions import parse_sections
        sections = parse_sections(missions_file.read_text())
        parts = []
        in_progress = sections.get("in_progress", [])
        pending = sections.get("pending", [])
        done = sections.get("done", [])
        if in_progress:
            parts.append("In progress:\n" + "\n".join(in_progress[:5]))
        if pending:
            parts.append(f"Pending ({len(pending)}):\n" + "\n".join(pending[:5]))
        if done:
            parts.append(f"Done: {len(done)}")
        if parts:
            missions_text = "\n\n".join(parts)

    pending_text = "No run in progress."
    pending_path = instance_dir / "journal" / "pending.md"
    if pending_path.exists():
        content = pending_path.read_text().strip()
        if content:
            if len(content) > 1500:
                pending_text = "...\n" + content[-1500:]
            else:
                pending_text = content

    return f"Quota:\n{usage_text}\n\nMissions:\n{missions_text}\n\nCurrent:\n{pending_text}"


def _handle_metrics(ctx) -> str:
    """Build mission metrics summary."""
    from app.mission_metrics import format_metrics_summary

    instance_dir = str(ctx.instance_dir)
    return format_metrics_summary(instance_dir, days=30)
