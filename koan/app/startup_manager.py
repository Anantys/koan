"""Startup orchestration for the Koan agent loop.

Decomposes the startup sequence into independently testable steps.
Each step is wrapped in try/except to prevent one failure from
blocking the entire startup.

Called from run.py's main_loop() during process initialization.
"""

import os
from pathlib import Path

from app.run_log import log


# ---------------------------------------------------------------------------
# Individual startup steps
# ---------------------------------------------------------------------------

def recover_crashed_missions(instance: str):
    """Check for and recover missions left in-progress by a crash."""
    log("health", "Checking for interrupted missions...")
    from app.recover import recover_missions
    recover_missions(instance)


def run_migrations(koan_root: str):
    """Auto-migrate env vars to projects.yaml (one-shot, idempotent)."""
    from app.projects_migration import run_migration
    migration_msgs = run_migration(koan_root)
    for msg in migration_msgs:
        log("init", f"[migration] {msg}")


def populate_github_urls(koan_root: str):
    """Auto-populate github_url in projects.yaml from git remotes."""
    from app.projects_config import ensure_github_urls
    gh_msgs = ensure_github_urls(koan_root)
    for msg in gh_msgs:
        log("init", f"[github-urls] {msg}")


def discover_workspace(koan_root: str, projects: list) -> list:
    """Initialize workspace + yaml merged project registry.

    Returns the refreshed project list.
    """
    from app.projects_merged import (
        refresh_projects,
        get_warnings,
        populate_workspace_github_urls,
        get_yaml_project_names,
    )

    projects = refresh_projects(koan_root)

    # Count workspace projects (not in projects.yaml)
    yaml_project_names = get_yaml_project_names(koan_root)
    ws_count = sum(1 for name, _ in projects if name not in yaml_project_names)
    if ws_count:
        log("init", f"[workspace] Discovered {ws_count} project(s) from workspace/")

    # Populate github_url cache for workspace projects
    gh_count = populate_workspace_github_urls(koan_root)
    if gh_count:
        log("init", f"[workspace] Cached {gh_count} github_url(s) from git remotes")

    # Log any warnings from merge
    for warning in get_warnings():
        log("warn", f"[workspace] {warning}")

    return projects


def validate_config(koan_root: str):
    """Validate config.yaml keys and types, warn on typos or bad values.

    Also detects config drift (keys in the template but missing from user config).
    """
    from app.utils import load_config
    from app.config_validator import validate_and_warn
    config = load_config()
    validate_and_warn(config, koan_root=koan_root)


def run_sanity_checks(instance: str):
    """Run all sanity checks from koan/sanity/."""
    log("health", "Running sanity checks...")
    from sanity import run_all
    for name, modified, changes in run_all(instance):
        if modified:
            for change in changes:
                log("health", f"  [{name}] {change}")


def _cleanup_marker_path() -> Path:
    """Return the path to the cleanup throttle marker file."""
    koan_root = os.environ.get("KOAN_ROOT", "")
    return Path(koan_root) / ".koan-last-cleanup" if koan_root else Path("/tmp/.koan-last-cleanup")


def _should_run_cleanup(max_age_hours: int = 24) -> bool:
    """Check if enough time has passed since the last cleanup.

    Returns True if cleanup should run (marker missing, corrupt, or older
    than max_age_hours).
    """
    marker = _cleanup_marker_path()
    if not marker.exists():
        return True
    try:
        timestamp = float(marker.read_text().strip())
    except (ValueError, OSError):
        return True
    import time
    elapsed_hours = (time.time() - timestamp) / 3600
    return elapsed_hours >= max_age_hours


def _write_cleanup_marker():
    """Write the current timestamp to the cleanup marker file."""
    import time
    marker = _cleanup_marker_path()
    try:
        from app.utils import atomic_write
        atomic_write(marker, str(time.time()))
    except OSError:
        pass


def cleanup_memory(instance: str):
    """Run memory compaction and cleanup.

    Throttled to once per 24 hours to avoid redundant work on fast restart
    cycles. On cold boot (summary.md missing but SNAPSHOT.md exists),
    hydrates memory from snapshot before running cleanup.
    """
    if not _should_run_cleanup():
        import time
        marker = _cleanup_marker_path()
        try:
            elapsed = (time.time() - float(marker.read_text().strip())) / 3600
            log("health", f"Memory cleanup skipped (last run {elapsed:.0f}h ago)")
        except (ValueError, OSError):
            log("health", "Memory cleanup skipped (recent run)")
        return

    log("health", "Running memory cleanup...")
    from app.memory_manager import MemoryManager
    mgr = MemoryManager(instance)

    # Cold-boot hydration: restore memory from snapshot if needed
    summary_missing = not mgr.summary_path.exists()
    snapshot_exists = (
        (mgr.memory_dir / "SNAPSHOT.md").exists()
        or (mgr.instance_dir / "SNAPSHOT.md").exists()
    )
    if summary_missing and snapshot_exists:
        log("health", "Cold boot detected — hydrating from SNAPSHOT.md...")
        restored = mgr.hydrate_from_snapshot()
        if restored:
            log("health", f"Hydrated {len(restored)} file(s) from snapshot")

    mgr.run_cleanup()
    _write_cleanup_marker()


def prune_missions_done(instance: str):
    """Prune old Done items from missions.md to keep file size bounded.

    missions.md grows unbounded as completed missions accumulate. At 190KB+,
    the agent wastes context tokens reading it. Keep only the last 50 Done items.
    """
    missions_path = Path(instance) / "missions.md"
    if not missions_path.exists():
        return

    from app.missions import prune_done_section
    from app.utils import atomic_write

    content = missions_path.read_text()
    new_content, pruned = prune_done_section(content, keep=50)
    if pruned > 0:
        atomic_write(missions_path, new_content)
        log("health", f"Pruned {pruned} old Done items from missions.md")


def cleanup_mission_history(instance: str):
    """Prune old entries from mission history."""
    from app.mission_history import cleanup_old_entries
    cleanup_old_entries(instance)


def check_health(koan_root: str, max_age: int = 120):
    """Check Telegram bridge health (heartbeat age)."""
    log("health", "Checking Telegram bridge health...")
    from app.health_check import check_and_alert
    check_and_alert(koan_root, max_age=max_age)


def check_self_reflection(instance: str):
    """Trigger periodic self-reflection if due and enabled in config.

    Controlled by the ``startup_reflection`` config key (default: false).
    When disabled, reflection is skipped at startup — it can still be
    triggered manually via the CLI entry point.
    """
    from app.config import get_startup_reflection
    if not get_startup_reflection():
        return

    log("health", "Checking self-reflection trigger...")
    from app.self_reflection import (
        should_reflect, run_reflection, save_reflection, notify_outbox,
    )
    inst_path = Path(instance)
    if should_reflect(inst_path):
        observations = run_reflection(inst_path)
        if observations:
            save_reflection(inst_path, observations)
            notify_outbox(inst_path, observations)


def handle_start_on_pause(koan_root: str):
    """Enter pause mode on startup if configured.

    Removes stale system-generated reason files (quota, max_runs)
    to prevent auto-resume from a previous session. Preserves
    manual pauses (user explicitly requested via /pause).

    Skipped when KOAN_SKIP_START_PAUSE=1 (set by /resume auto-restart
    to avoid immediately re-pausing the freshly launched runner).
    """
    if os.environ.get("KOAN_SKIP_START_PAUSE") == "1":
        log("pause", "start_on_pause skipped (KOAN_SKIP_START_PAUSE=1)")
        return

    from app.utils import get_start_on_pause

    if not get_start_on_pause():
        return

    from app.pause_manager import create_pause, get_pause_state, is_paused

    koan_root_path = Path(koan_root)
    if is_paused(koan_root):
        # Preserve manual pauses; clear stale non-manual pauses and re-create
        # a clean pause file (no lingering auto-resume reason).
        state = get_pause_state(koan_root)
        if state and state.reason != "manual":
            create_pause(koan_root, "start_on_pause")
    else:
        log("pause", "start_on_pause=true in config. Entering pause mode.")
        create_pause(koan_root, "start_on_pause")


def handle_start_passive(koan_root: str):
    """Enter passive mode on startup if configured.

    When start_passive=true in config.yaml, creates .koan-passive with no
    duration (indefinite). Requires explicit /active to resume.
    No-op if already passive.
    """
    from app.config import get_start_passive

    if not get_start_passive():
        return

    from app.passive_manager import is_passive, create_passive

    if is_passive(koan_root):
        return  # already passive, don't overwrite

    log("passive", "start_passive=true in config. Entering passive mode.")
    create_passive(koan_root, duration=0, reason="start_passive")


def setup_git_identity():
    """Set git author/committer from KOAN_EMAIL env var."""
    koan_email = os.environ.get("KOAN_EMAIL", "")
    if koan_email:
        os.environ["GIT_AUTHOR_NAME"] = "Kōan"
        os.environ["GIT_AUTHOR_EMAIL"] = koan_email
        os.environ["GIT_COMMITTER_NAME"] = "Kōan"
        os.environ["GIT_COMMITTER_EMAIL"] = koan_email


def setup_github_auth():
    """Authenticate GitHub CLI if GITHUB_USER is set."""
    if not os.environ.get("GITHUB_USER"):
        return
    from app.github_auth import setup_github_auth as _setup_auth
    success = _setup_auth()
    if success:
        log("init", f"GitHub CLI authenticated as {os.environ['GITHUB_USER']}")
    else:
        log("init", f"Warning: GitHub auth failed for {os.environ['GITHUB_USER']}")


def run_git_sync(instance: str, projects: list):
    """Sync all project branches with their remotes."""
    log("git", "Running git sync...")
    from app.git_sync import GitSync
    for name, path in projects:
        try:
            gs = GitSync(instance, name, path)
            gs.sync_and_report()
        except Exception as e:
            log("error", f"Git sync failed for {name}: {e}")


def run_daily_report():
    """Send daily report if due."""
    from app.daily_report import send_daily_report
    send_daily_report()


def check_auto_update(koan_root: str, instance: str) -> bool:
    """Check for upstream updates and trigger pull + restart if available.

    Returns True if an update was triggered (process will restart).
    """
    from app.auto_update import perform_auto_update
    return perform_auto_update(koan_root, instance)


def run_morning_ritual(instance: str):
    """Execute the morning ritual."""
    log("init", "Running morning ritual...")
    from app.rituals import run_ritual
    run_ritual("morning", Path(instance))


# ---------------------------------------------------------------------------
# Safe step runner
# ---------------------------------------------------------------------------

def _safe_run(step_name: str, fn, *args, **kwargs):
    """Run a startup step, catching and logging any exception."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        log("error", f"{step_name} failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def run_startup(koan_root: str, instance: str, projects: list):
    """Run all startup tasks (crash recovery, health, sync, etc.).

    Returns (max_runs, interval, branch_prefix) configuration tuple.
    """
    from app.banners import print_agent_banner
    from app.utils import (
        get_branch_prefix,
        get_cli_binary_for_shell,
        get_interval_seconds,
        get_max_runs,
    )

    # Load config
    max_runs = get_max_runs()
    interval = get_interval_seconds()
    branch_prefix = get_branch_prefix()
    cli_provider = get_cli_binary_for_shell()

    # Print banner
    try:
        print_agent_banner(f"agent loop — {cli_provider}")
    except Exception as e:
        log("error", f"Banner display failed: {e}")

    # --- Protected startup checks ---
    # Import protected_phase lazily to avoid circular import
    # (run.py imports startup_manager, signal handling lives in run.py)
    from app.run import protected_phase

    with protected_phase("Startup checks"):
        _safe_run("Config validation", validate_config, koan_root)
        _safe_run("Crash recovery", recover_crashed_missions, instance)
        _safe_run("Projects migration", run_migrations, koan_root)
        _safe_run("GitHub URL population", populate_github_urls, koan_root)

        result = _safe_run("Workspace discovery", discover_workspace, koan_root, projects)
        if result is not None:
            projects = result

        _safe_run("Sanity checks", run_sanity_checks, instance)
        _safe_run("Memory cleanup", cleanup_memory, instance)
        _safe_run("Missions pruning", prune_missions_done, instance)
        _safe_run("Mission history cleanup", cleanup_mission_history, instance)
        _safe_run("Health check", check_health, koan_root)

    with protected_phase("Self-reflection check"):
        _safe_run("Self-reflection check", check_self_reflection, instance)

    # Start on pause / passive
    _safe_run("Start on pause", handle_start_on_pause, koan_root)
    _safe_run("Start passive", handle_start_passive, koan_root)

    # Git identity and GitHub auth
    _safe_run("Git identity", setup_git_identity)
    _safe_run("GitHub auth", setup_github_auth)

    # Startup notification
    log("init", f"Starting. Max runs: {max_runs}, interval: {interval}s")

    # Import status/notify helpers lazily from run
    from app.run import set_status, _build_startup_status, _notify

    project_list = "\n".join(f"  • {n}" for n, _ in sorted(projects))
    current_project = projects[0][0] if projects else "none"
    status_line = _build_startup_status(koan_root)
    set_status(koan_root, status_line)
    _notify(instance, (
        f"Kōan starting — {max_runs} max runs, {interval}s interval.\n"
        f"Projects:\n{project_list}\n"
        f"Current: {current_project}.\n"
        f"Status: {status_line}"
    ))

    with protected_phase("Git sync"):
        run_git_sync(instance, projects)

    # Auto-update check (before daily report / morning ritual)
    updated = _safe_run("Auto-update check", check_auto_update, koan_root, instance)
    if updated:
        # Restart signal has been set — exit to let wrapper restart us
        import sys
        from app.restart_manager import RESTART_EXIT_CODE
        sys.exit(RESTART_EXIT_CODE)

    # Daily report
    _safe_run("Daily report", run_daily_report)

    with protected_phase("Morning ritual"):
        _safe_run("Morning ritual", run_morning_ritual, instance)

    # Initialize hook system and fire session_start
    from app.hooks import fire_hook, init_hooks
    _safe_run("Hook discovery", init_hooks, instance)
    _safe_run("Session start hooks", fire_hook, "session_start",
              instance_dir=instance, koan_root=koan_root)

    return max_runs, interval, branch_prefix
