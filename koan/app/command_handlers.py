"""Telegram bridge command handlers.

All /command handlers live here. Extracted from awake.py to keep
the main module focused on polling, chat, and outbox.

This module uses callback injection for handle_chat and _run_in_worker
to avoid circular imports with awake.py.
"""

import time
from typing import Callable, Optional

from app.bridge_log import log
from app.bridge_state import (
    KOAN_ROOT,
    INSTANCE_DIR,
    MISSIONS_FILE,
    _get_registry,
    _reset_registry,
)
from app.notify import TypingIndicator, send_telegram
from app.signals import PAUSE_FILE, QUOTA_RESET_FILE, STOP_FILE
from app.skills import Skill, SkillContext, SkillError, execute_skill
from app.utils import (
    parse_project as _parse_project,
    detect_project_from_text,
    get_known_projects,
    insert_pending_mission,
    is_known_project,
)

# Callbacks injected by awake.py at startup to avoid circular imports
_handle_chat_cb: Optional[Callable] = None
_run_in_worker_cb: Optional[Callable] = None


def set_callbacks(
    handle_chat: Callable,
    run_in_worker: Callable,
):
    """Inject callbacks from awake.py (called once at import time)."""
    global _handle_chat_cb, _run_in_worker_cb
    _handle_chat_cb = handle_chat
    _run_in_worker_cb = run_in_worker


# Core commands that remain hardcoded (safety-critical or bootstrap)
CORE_COMMANDS = frozenset({
    "help", "stop", "sleep", "resume", "skill",
    "pause", "work", "awake", "start", "run",  # aliases for sleep/resume
})



def handle_command(text: str):
    """Handle /commands — core commands hardcoded, rest via skills."""
    cmd = text.strip().lower()

    # --- Core hardcoded commands (safety-critical / bootstrap) ---

    if cmd == "/stop":
        from app.utils import atomic_write
        atomic_write(KOAN_ROOT / STOP_FILE, "STOP")
        send_telegram("⏹️ Stop requested. Current mission will complete, then Kōan will stop.")
        return

    if cmd in ("/pause", "/sleep"):
        from app.pause_manager import is_paused, create_pause
        if is_paused(str(KOAN_ROOT)):
            send_telegram("⏸️ Already paused. /resume to unpause.")
        else:
            create_pause(str(KOAN_ROOT), reason="manual", display="paused via Telegram")
            send_telegram("⏸️ Paused. No missions will run. /resume to unpause.")
        return

    if cmd in ("/resume", "/work", "/awake", "/run"):
        handle_resume()
        return

    if cmd == "/start":
        _handle_start()
        return

    if cmd == "/help" or cmd.startswith("/help "):
        help_args = text.strip()[5:].strip()  # everything after "/help"
        if help_args:
            _handle_help_detail(help_args)
        else:
            _handle_help()
        return

    if cmd.startswith("/skill"):
        _handle_skill_command(text[6:].strip())
        return

    # --- Skill-based dispatch ---

    # Extract command name and args from /command_name args
    parts = text.strip().split(None, 1)
    command_name = parts[0][1:].lower()  # strip the /
    command_args = parts[1] if len(parts) > 1 else ""

    # Aliases are handled by the skill registry (SKILL.md aliases: field)
    # No hardcoded alias remapping needed here.

    registry = _get_registry()
    skill = registry.find_by_command(command_name)

    if skill is not None:
        _dispatch_skill(skill, command_name, command_args)
        return

    # Scoped command dispatch: /<scope>.<name> [args]
    # e.g., /anantys.review or /core.status.ping
    if "." in command_name:
        resolved = registry.resolve_scoped_command(
            command_name + (" " + command_args if command_args else "")
        )
        if resolved:
            skill, subcommand, skill_args = resolved
            _dispatch_skill(skill, subcommand, skill_args)
            return

    # Project-name fallback: if the "command" is actually a known project name,
    # rewrite as "/mission <project> <context>" so the user can write e.g.
    # "/koan fix the bug" instead of "/mission koan fix the bug".
    # This is the very last fallback to avoid collision with existing skills.
    if is_known_project(command_name) and command_args:
        handle_mission(f"{command_name} {command_args}")
        return

    # Group-name fallback: /<group> → /help <group>
    if command_name in _GROUP_META:
        _handle_help_group(command_name, registry)
        return

    # Unknown command — reject immediately instead of wasting LLM credits
    suggestion = registry.suggest_command(command_name, extra_commands=list(CORE_COMMANDS))
    hint = f"\nDid you mean /{suggestion}?" if suggestion else ""
    send_telegram(f"❌ Unknown command: /{command_name}{hint}\nUse /help to see available commands.")


def _dispatch_skill(skill: Skill, command_name: str, command_args: str):
    """Dispatch a skill execution — handles worker threads and standard calls."""
    # cli_skill + audience:agent → queue as mission for the runner, don't execute inline
    if skill.cli_skill and skill.audience == "agent":
        _queue_cli_skill_mission(skill, command_args)
        return

    ctx = SkillContext(
        koan_root=KOAN_ROOT,
        instance_dir=INSTANCE_DIR,
        command_name=command_name,
        args=command_args,
        send_message=send_telegram,
        handle_chat=_handle_chat_cb,
    )

    # Worker thread for blocking skills (calls Claude or external services)
    if skill.worker:
        if not _run_in_worker_cb:
            log("error", f"Worker callback not set — cannot run skill '{command_name}'")
            send_telegram(f"Cannot run /{command_name} — worker thread not available.")
            return
        def _run_skill():
            try:
                with TypingIndicator():
                    result = execute_skill(skill, ctx)
                _handle_skill_result(result, command_name, command_args)
            except Exception as e:
                log("error", f"Worker skill '{command_name}' failed: {e}")
                try:
                    send_telegram(f"/{command_name} failed: {type(e).__name__}: {e}")
                except Exception as notify_err:
                    log("error", f"Failed to notify user about '{command_name}' error: {notify_err}")
        _run_in_worker_cb(_run_skill)
        return

    # Standard skill execution
    result = execute_skill(skill, ctx)
    _handle_skill_result(result, command_name, command_args)


def _handle_skill_result(result, command_name: str, command_args: str):
    """Handle the result of a skill execution, logging errors and sending responses."""
    if isinstance(result, SkillError):
        log("error", f"Skill handler '{command_name}' crashed: {result.exception}")
        send_telegram(result.message)
    elif result is not None:
        from app.text_utils import expand_github_refs_auto
        send_telegram(expand_github_refs_auto(result, command_args))


def _queue_cli_skill_mission(skill: Skill, args: str):
    """Queue a cli_skill mission for the runner to execute via the CLI provider."""
    from app.utils import get_known_projects

    # Try to extract project from the first word of args (case-insensitive,
    # matching detect_project_from_text() behavior in utils.py).
    project = None
    mission_args = args
    words = args.split(None, 1)
    if words:
        known_map = {name.lower(): name for name, _ in get_known_projects()}
        matched = known_map.get(words[0].lower())
        if matched:
            project = matched
            mission_args = words[1] if len(words) > 1 else ""

    # Reconstruct the Kōan scoped command
    koan_cmd = f"/{skill.scope}.{skill.name}"
    if mission_args:
        koan_cmd += f" {mission_args}"

    # Format mission entry with optional project tag
    if project:
        entry = f"- [project:{project}] {koan_cmd}"
    else:
        entry = f"- {koan_cmd}"

    insert_pending_mission(MISSIONS_FILE, entry)

    ack = f"✅ Mission queued"
    if project:
        ack += f" (project: {project})"
    ack += f":\n\n{koan_cmd[:500]}"
    send_telegram(ack)


def _handle_skill_command(args: str):
    """Handle /skill — list skills, manage sources, or invoke a specific one.

    Usage:
        /skill                           — list all skills
        /skill core                      — list skills in scope 'core'
        /skill core.status               — invoke core/status skill
        /skill core.status.ping          — invoke subcommand 'ping'
        /skill install <url> [scope]     — install skills from a Git repo
        /skill update [scope]            — update one or all sources
        /skill remove <scope>            — remove an installed source
        /skill sources                   — list installed sources
    """
    registry = _get_registry()

    # --- Skill management subcommands ---
    if args:
        sub_parts = args.split(None, 1)
        sub_cmd = sub_parts[0].lower()
        sub_args = sub_parts[1] if len(sub_parts) > 1 else ""

        if sub_cmd == "install":
            _handle_skill_install(sub_args)
            return

        if sub_cmd == "update":
            _handle_skill_update(sub_args)
            return

        if sub_cmd == "remove":
            _handle_skill_remove(sub_args)
            return

        if sub_cmd == "sources":
            _handle_skill_sources()
            return

    if not args:
        # List non-core skills grouped by scope (core skills are in /help)
        non_core = [s for s in registry.list_all() if s.scope != "core"]
        if not non_core:
            send_telegram("ℹ️ No extra skills loaded. Core skills are listed in /help.\n\nInstall with: /skill install <git-url> [scope]")
            return

        parts = ["Available Skills\n"]
        non_core_scopes = sorted(set(s.scope for s in non_core))
        for scope in non_core_scopes:
            parts.append(f"{scope}")
            for skill in registry.list_by_scope(scope):
                for cmd in skill.commands:
                    desc = cmd.description or skill.description
                    parts.append(f"  /{scope}.{cmd.name} -- {desc}")
            parts.append("")

        parts.append("Use: /<scope>.<name> [args]")
        parts.append("Manage: /skill install|update|remove|sources")
        send_telegram("\n".join(parts))
        return

    # Parse skill reference: scope.name[.subcommand] [args]
    ref_parts = args.split(None, 1)
    ref = ref_parts[0]
    skill_args = ref_parts[1] if len(ref_parts) > 1 else ""

    segments = ref.split(".")

    if len(segments) == 1:
        # Just a scope — list skills in that scope
        scope_name = segments[0]
        scope_skills = registry.list_by_scope(scope_name)
        if not scope_skills:
            send_telegram(f"ℹ️ No skills found in scope '{scope_name}'.")
            return
        # Use /command for core skills, /<scope>.<command> for others
        prefix = "" if scope_name == "core" else f"{scope_name}."
        parts = [f"Skills in {scope_name}\n"]
        for skill in scope_skills:
            for cmd in skill.commands:
                desc = cmd.description or skill.description
                parts.append(f"  /{prefix}{cmd.name} -- {desc}")
        send_telegram("\n".join(parts))
        return

    # Use resolve_scoped_command for consistent lookup (by skill name + command name fallback)
    resolved = registry.resolve_scoped_command(args)
    if resolved is None:
        scope = segments[0]
        skill_name = segments[1]
        send_telegram(f"❌ Skill '{scope}.{skill_name}' not found. /skill to list available skills.")
        return

    skill, subcommand, skill_args = resolved
    _dispatch_skill(skill, subcommand, skill_args)


def _handle_skill_install(args: str):
    """Handle /skill install <url> [scope] [--ref=<ref>]."""
    from app.skill_manager import install_skill_source

    if not args:
        send_telegram(
            "Usage: /skill install <git-url> [scope] [--ref=tag]\n\n"
            "Examples:\n"
            "  /skill install myorg/koan-skills-ops\n"
            "  /skill install https://github.com/team/skills.git ops\n"
            "  /skill install myorg/skills ops --ref=v1.0.0"
        )
        return

    parts = args.split()
    url = parts[0]
    scope = None
    ref = "main"

    for part in parts[1:]:
        if part.startswith("--ref="):
            ref = part[6:]
        elif scope is None:
            scope = part

    ok, msg = install_skill_source(INSTANCE_DIR, url, scope=scope, ref=ref)
    if ok:
        _reset_registry()  # Reload skills
    send_telegram(f"{'✅' if ok else '❌'} {msg}")


def _handle_skill_update(args: str):
    """Handle /skill update [scope]."""
    from app.skill_manager import update_skill_source, update_all_sources

    scope = args.strip()
    if scope:
        ok, msg = update_skill_source(INSTANCE_DIR, scope)
    else:
        ok, msg = update_all_sources(INSTANCE_DIR)

    if ok:
        _reset_registry()  # Reload skills
    send_telegram(msg)


def _handle_skill_remove(args: str):
    """Handle /skill remove <scope>."""
    from app.skill_manager import remove_skill_source

    scope = args.strip()
    if not scope:
        send_telegram("Usage: /skill remove <scope>")
        return

    ok, msg = remove_skill_source(INSTANCE_DIR, scope)
    if ok:
        _reset_registry()  # Reload skills
    send_telegram(f"{'✅' if ok else '❌'} {msg}")


def _handle_skill_sources():
    """Handle /skill sources — list installed skill sources."""
    from app.skill_manager import list_sources

    send_telegram(list_sources(INSTANCE_DIR))


# Group display metadata: emoji + short description for /help L1
_GROUP_META = {
    "missions": ("📋", "Create, list, cancel missions"),
    "code":     ("🔧", "Review, refactor, PR, fix, implement"),
    "pr":       ("🔀", "Pull request management"),
    "status":   ("📊", "System state, quota, logs"),
    "config":   ("⚙️", "Projects, language, focus, verbose"),
    "ideas":    ("💡", "Ideas, reflection, sparring"),
    "system":   ("🔄", "Pause, stop, update, restart"),
}

# Core commands that are hardcoded (not skill-based) but should appear in /help.
# Each entry: (command_name, description, aliases, group)
_CORE_COMMAND_HELP = [
    ("help",   "Show help overview or details",   ["h"],                    "system"),
    ("stop",   "Stop the run loop",               [],                      "system"),
    ("pause",  "Pause mission processing",         ["sleep"],              "system"),
    ("resume", "Resume mission processing",        ["work", "awake", "run", "start"], "system"),
    ("skill",  "Manage skill packages",            [],                     "system"),
]

# Ordered group list (controls display order in /help)
_GROUP_ORDER = [
    "missions", "code", "pr", "status",
    "config", "ideas", "system",
]


def _handle_help_detail(arg: str):
    """Handle /help <arg> — L2 group expansion or L3 command help."""
    arg = arg.lstrip("/").lower()

    registry = _get_registry()

    # L2: check if arg is a group name
    if arg in _GROUP_META:
        _handle_help_group(arg, registry)
        return

    # L3: check if arg is a skill-based command
    skill = registry.find_by_command(arg)
    if skill is not None:
        _handle_help_command(arg, skill)
        return

    # L3: check if arg is a hardcoded core command (or alias)
    for cmd_name, desc, aliases, _group in _CORE_COMMAND_HELP:
        if arg == cmd_name or arg in aliases:
            parts = [f"/{cmd_name}", desc]
            if aliases:
                parts.append(f"Aliases: /{', /'.join(aliases)}")
            send_telegram("\n".join(parts))
            return

    # Unknown — suggest closest match from commands AND groups
    suggestion = registry.suggest_command(arg, extra_commands=list(CORE_COMMANDS) + list(_GROUP_META.keys()))
    hint = f"\nDid you mean /{suggestion}?" if suggestion else ""
    send_telegram(f"Unknown command: /{arg}{hint}\nUse /help to see all groups.")


def _handle_help_group(group: str, registry):
    """L2: Show all commands in a group."""
    emoji, description = _GROUP_META[group]
    parts = [f"{emoji} {group.title()} — {description}\n"]

    skills = registry.list_by_group(group)
    for skill in skills:
        for cmd in skill.commands:
            desc = cmd.description or skill.description
            aliases = f" (alias: /{', /'.join(cmd.aliases)})" if cmd.aliases else ""
            parts.append(f"/{cmd.name} — {desc}{aliases}")

    # Append hardcoded core commands belonging to this group
    for cmd_name, desc, aliases, cmd_group in _CORE_COMMAND_HELP:
        if cmd_group == group:
            alias_str = f" (alias: /{', /'.join(aliases)})" if aliases else ""
            parts.append(f"/{cmd_name} — {desc}{alias_str}")

    parts.append(f"\n/help <command> — detailed usage")
    send_telegram("\n".join(parts))


def _handle_help_command(command_name: str, skill=None):
    """L3: Show help for a specific command."""
    if skill is None:
        registry = _get_registry()
        skill = registry.find_by_command(command_name)

    if skill is None:
        send_telegram(f"Unknown command: /{command_name}\nUse /help to see all groups.")
        return

    cmd = next(
        (c for c in skill.commands
         if c.name == command_name or command_name in c.aliases),
        None,
    )
    if cmd is None:
        send_telegram(f"Unknown command: /{command_name}\nUse /help to see all groups.")
        return

    parts = [f"/{cmd.name}"]
    desc = cmd.description or skill.description
    if desc:
        parts.append(desc)
    if cmd.aliases:
        parts.append(f"Aliases: /{', /'.join(cmd.aliases)}")
    if cmd.usage:
        parts.append(f"Usage: {cmd.usage}")
    else:
        parts.append("No usage defined.")

    send_telegram("\n".join(parts))


def _handle_help():
    """L1: Send grouped overview — max ~12 lines."""
    registry = _get_registry()

    parts = ["Kōan — Help\n"]

    # Show groups with command count
    for group in _GROUP_ORDER:
        if group not in _GROUP_META:
            continue
        emoji, description = _GROUP_META[group]
        parts.append(f"{emoji} /{group} — {description}")

    # Dynamic groups from custom skills not in _GROUP_ORDER
    for group in registry.groups():
        if group not in _GROUP_META:
            count = len(registry.list_by_group(group))
            parts.append(f"📦 {group} — {count} command{'s' if count != 1 else ''}")

    # Non-core skills section (if any installed)
    non_core = [s for s in registry.list_all() if s.scope != "core"]
    if non_core:
        parts.append(f"\n/skill — {len(non_core)} extra skill{'s' if len(non_core) != 1 else ''}")

    parts.extend([
        "",
        "/help <group> — expand a group",
        "/help <command> — command details",
        "",
        'Send a message to chat, or "mission: <text>" to queue work.',
    ])
    send_telegram("\n".join(parts))


def _reset_session_counters():
    """Reset internal usage session counters after quota resume.

    When the human hits /resume after a quota pause, they've verified
    that API quota is available. The internal token counter (usage_state.json)
    may still show a high percentage from the exhausted session. Resetting
    it prevents the run loop from immediately re-pausing with stale data.

    The real quota gate (quota_handler.py) will catch actual exhaustion
    reactively from Claude CLI output if it occurs.
    """
    try:
        from pathlib import Path
        from app.usage_estimator import cmd_reset_session
        usage_state = Path(INSTANCE_DIR, "usage_state.json")
        usage_md = Path(INSTANCE_DIR, "usage.md")
        cmd_reset_session(usage_state, usage_md)
        log("health", "Session counters reset after quota resume")
    except Exception as e:
        log("error", f"Failed to reset session counters: {e}")


def _is_runner_alive() -> bool:
    """Check if the run process is alive via its PID file."""
    from app.pid_manager import check_pidfile
    return check_pidfile(KOAN_ROOT, "run") is not None


def _auto_restart_runner() -> bool:
    """Start the runner with start_on_pause bypassed.

    Returns True if the runner was successfully started.
    """
    from app.pid_manager import start_runner
    ok, msg = start_runner(
        KOAN_ROOT, extra_env={"KOAN_SKIP_START_PAUSE": "1"},
    )
    if ok:
        send_telegram(f"🔄 Runner was not active — restarting now… {msg}")
    else:
        send_telegram(f"❌ Failed to restart runner: {msg}")
    return ok


def handle_resume():
    """Resume from pause or quota exhaustion.

    If the run process is dead, automatically restarts it with
    KOAN_SKIP_START_PAUSE=1 so start_on_pause doesn't immediately
    re-pause the freshly launched runner.
    """
    from app.pause_manager import get_pause_state, remove_pause

    pause_file = KOAN_ROOT / PAUSE_FILE
    quota_file = KOAN_ROOT / QUOTA_RESET_FILE  # Legacy, kept for compat

    if pause_file.exists():
        # Read pause reason and reset info for better messaging
        state = get_pause_state(str(KOAN_ROOT))
        reason = state.reason if state else "manual"
        reset_timestamp = state.timestamp if state and state.timestamp else None
        reset_display = state.display if state else ""

        remove_pause(str(KOAN_ROOT))

        if reason == "quota":
            # Reset internal session counters so the estimator doesn't
            # immediately re-pause with stale high usage percentage
            _reset_session_counters()

            # Check if we're resuming before the reset time
            if reset_timestamp and time.time() < reset_timestamp:
                from app.reset_parser import time_until_reset
                remaining = time_until_reset(reset_timestamp)
                send_telegram(
                    f"▶️ Unpaused (was: quota exhausted). "
                    f"Note: estimated reset in ~{remaining}. "
                    f"Internal counters cleared — will rely on real API feedback. "
                    f"If quota is still exhausted, I'll detect it and pause again with details."
                )
            else:
                send_telegram(
                    "▶️ Unpaused (was: quota exhausted). "
                    "Quota should be reset. Internal counters cleared. "
                    "Resuming main loop."
                )
        elif reason == "max_runs":
            send_telegram("▶️ Unpaused (was: max_runs). Run counter reset, loop continues.")
        else:
            send_telegram("▶️ Unpaused. Missions resume next cycle.")

        # If the runner died while paused, restart it automatically
        if not _is_runner_alive():
            _auto_restart_runner()
        return

    # Legacy fallback: old .koan-quota-reset file (can be removed in future)
    if not quota_file.exists():
        # No pause state, but runner might be dead — restart it
        if not _is_runner_alive():
            _auto_restart_runner()
        else:
            send_telegram("ℹ️ No pause or quota hold detected. /status to check.")
        return

    try:
        lines = quota_file.read_text().strip().split("\n")
        reset_info = lines[0] if lines else "unknown time"
        paused_at = 0
        if len(lines) > 1 and lines[1].strip():
            try:
                paused_at = int(lines[1].strip())
            except ValueError:
                pass

        hours_since_pause = (time.time() - paused_at) / 3600
        likely_reset = hours_since_pause >= 2

        if likely_reset:
            quota_file.unlink(missing_ok=True)
            send_telegram(f"▶️ Quota likely reset ({reset_info}, paused {hours_since_pause:.1f}h ago). Restart with: make run")
        else:
            send_telegram(f"⏳ Quota not reset yet ({reset_info}). Paused {hours_since_pause:.1f}h ago. Check back later.")
    except (OSError, ValueError) as e:
        log("error", f"Error checking quota reset: {e}")
        send_telegram("⚠️ Error checking quota. /status or check manually.")


def _handle_start():
    """Start the agent loop — smart command that handles both cases.

    If the runner is stopped: clears .koan-stop, launches run.py.
    If the runner is paused: behaves like /resume.
    If the runner is running: tells the user.
    """
    from app.pid_manager import check_pidfile, start_runner

    pid = check_pidfile(KOAN_ROOT, "run")
    if pid:
        # Runner is alive — check if paused
        pause_file = KOAN_ROOT / PAUSE_FILE
        if pause_file.exists():
            handle_resume()
        else:
            send_telegram(f"ℹ️ Agent loop already running (PID {pid}). Nothing to do.")
        return

    # Runner is stopped — launch it
    send_telegram("🚀 Starting agent loop...")
    ok, msg = start_runner(KOAN_ROOT)
    if ok:
        send_telegram(f"✅ {msg}")
    else:
        send_telegram(f"❌ {msg}")


def _quarantine_mission(text: str, reason: str, source: str = "unknown"):
    """Write a blocked/flagged mission to the quarantine file for human review."""
    from datetime import datetime

    quarantine_path = INSTANCE_DIR / "missions-quarantine.md"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"- 🛡️ [{timestamp}] ({source}) {reason}: {text[:500]}\n"
    try:
        with open(quarantine_path, "a") as f:
            f.write(entry)
    except OSError:
        log("guard", f"Failed to write quarantine entry: {reason}")


def handle_mission(text: str):
    """Append to missions.md with optional project tag."""
    from app.missions import extract_now_flag, sanitize_mission_text

    # Sanitize multi-line input (e.g. from Telegram) into a single line
    text = sanitize_mission_text(text)

    # Check for --now flag in first 5 words (queue at top instead of bottom)
    urgent, text = extract_now_flag(text)

    # Parse project tag if present
    project, mission_text = _parse_project(text)

    # Auto-detect project from first word (e.g. "koan do something")
    if not project:
        project, detected_text = detect_project_from_text(text)
        if project:
            mission_text = detected_text

    # Clean up the mission prefix
    if mission_text.lower().startswith("mission:"):
        mission_text = mission_text[8:].strip()
    elif mission_text.lower().startswith("mission :"):
        mission_text = mission_text[9:].strip()

    # Scan for prompt injection before queuing
    from app.prompt_guard import scan_mission_text
    from app.config import get_prompt_guard_config

    guard_config = get_prompt_guard_config()
    if guard_config["enabled"]:
        guard_result = scan_mission_text(mission_text)
        if guard_result.blocked:
            if guard_config["block_mode"]:
                send_telegram(
                    f"🛡️ Mission blocked — suspicious content detected: {guard_result.reason}"
                )
                log("guard", f"BLOCKED mission: {guard_result.reason} | {mission_text[:100]}")
                _quarantine_mission(mission_text, guard_result.reason, source="telegram")
                return
            else:
                send_telegram(
                    f"⚠️ Warning — mission queued but flagged: {guard_result.reason}"
                )
                log("guard", f"WARNING mission: {guard_result.reason} | {mission_text[:100]}")
                _quarantine_mission(mission_text, guard_result.reason, source="telegram")

    # Format mission entry with project tag if specified
    if project:
        mission_entry = f"- [project:{project}] {mission_text}"
    else:
        mission_entry = f"- {mission_text}"

    # Append to missions.md under pending section (with file locking)
    insert_pending_mission(MISSIONS_FILE, mission_entry, urgent=urgent)

    # Acknowledge with project info
    ack_msg = "✅ Mission received"
    if urgent:
        ack_msg += " (priority)"
    if project:
        ack_msg += f" (project: {project})"
    ack_msg += f":\n\n{mission_text[:500]}"
    send_telegram(ack_msg)
    log("mission", f"Mission queued: [{project or 'default'}] {mission_text[:60]}")
