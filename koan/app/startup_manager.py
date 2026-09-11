"""Startup orchestration for the Koan agent loop.

Decomposes the startup sequence into independently testable steps.
Each step is wrapped in try/except to prevent one failure from
blocking the entire startup.

Called from run.py's main_loop() during process initialization.
"""

import os
import time
from pathlib import Path

from app.config import get_configured_messaging_level_explicit
from app.run_log import log


def _messaging_notice_sentinel(instance: str) -> Path:
    return Path(instance) / ".messaging-level-notice-sent"


def _notify_raw(instance, msg):
    # Send straight to Telegram, skipping the Claude-CLI personality
    # reformatter (mirrors run.py::_notify_raw). The advisory contains literal
    # instructions (/messaging_level debug, messaging.level: debug) the
    # reformatter could garble, and an extra Claude CLI call at startup is
    # wasteful — especially when quota is exhausted. Exceptions propagate so the
    # caller's sentinel is written only after a successful send (retry next boot).
    from app.notify import send_telegram
    send_telegram(msg)


def maybe_send_messaging_level_notice(instance: str) -> None:
    """One-time advisory that the bridge now defaults to 'normal'.

    Fires once (sentinel-gated). Skipped — and the sentinel marked — when the
    operator has explicitly set messaging.level in config.yaml. The sentinel is
    written only after a successful send so a crash mid-send retries next boot.
    """
    sentinel = _messaging_notice_sentinel(instance)
    if sentinel.exists():
        return
    if get_configured_messaging_level_explicit() is not None:
        sentinel.write_text("skipped: explicitly configured\n")  # don't nag later
        return
    _notify_raw(
        instance,
        "🔉 Heads up: the bridge is now quieter by default (messaging.level = "
        "normal). You'll still get failures, command replies, and one-line "
        "PR results. To restore the full firehose, run /messaging_level debug "
        "or set messaging.level: debug in config.yaml.",
    )
    sentinel.write_text("sent\n")  # only after successful send


# ---------------------------------------------------------------------------
# Individual startup steps
# ---------------------------------------------------------------------------

def migrate_memory_to_jsonl(instance: str):
    """One-shot migration from markdown memory to JSONL truth log.

    Gated by a sentinel file — subsequent calls are no-ops.
    """
    from app.memory_manager import MemoryManager
    mgr = MemoryManager(instance)
    result = mgr.migrate_markdown_to_jsonl()
    if not result.get("skipped"):
        sessions = result.get("sessions", 0)
        learnings = result.get("learnings", 0)
        log("init", f"[memory] Migrated to JSONL: {sessions} sessions, {learnings} learnings")


def index_memory_sqlite(instance: str):
    """Bulk-index the JSONL memory log into the SQLite FTS5 secondary index.

    Idempotent and always-run: ``migrate_jsonl_to_sqlite`` self-gates on a
    missing or empty ``memory.db`` and returns 0 once the index is populated,
    so re-running on every startup is cheap. This must NOT be folded into
    ``migrate_markdown_to_jsonl`` — that method is gated by the
    ``.migration_done`` sentinel and skips entirely for already-migrated
    instances, which would leave their FTS5 index permanently empty.
    """
    from app.memory_db import migrate_jsonl_to_sqlite
    count = migrate_jsonl_to_sqlite(instance)
    if count:
        log("init", f"[memory] Indexed {count} JSONL entries into SQLite FTS5")


def recover_crashed_missions(instance: str):
    """Check for and recover missions left in-progress by a crash."""
    log("health", "Checking for interrupted missions...")
    from app.recover import recover_missions
    recover_missions(instance)


def _current_active_titles(instance: str):
    """Titles currently Pending or In Progress in the mission export.

    Returns a set of mission-line strings, or ``None`` if the export cannot be
    read/parsed — the caller then skips reconciliation (fail-open) rather than
    treating "couldn't read" as "nothing active" and wrongly clearing every
    live indicator. Called after crash recovery, so a requeued mission is back
    in Pending and correctly keeps its indicator until it re-runs.
    """
    import os

    from app import missions

    path = os.path.join(instance, "missions.md")
    if not os.path.exists(path):
        return set()
    try:
        with open(path, encoding="utf-8") as fh:
            content = fh.read()
        sections = missions.parse_sections(content)
        titles = set()
        for key in ("pending", "in_progress"):
            titles.update(sections.get(key, []))
        return titles
    except (OSError, ValueError):
        return None


def reconcile_running_indicators(instance: str) -> None:
    """Tear down stale GitHub 'Running' indicators left by a hard crash.

    A SIGKILL / power loss skips ``_finalize_mission``'s teardown, stranding a
    yellow ``pending`` status and a ``koan:working`` label. Resolve any tracked
    mission that is no longer active as an ``error``.
    """
    active = _current_active_titles(instance)
    if active is None:
        return  # could not read active set — do not wrongly clear
    from app.mission_status import reconcile_stale_indicators
    reconcile_stale_indicators(instance, active)


def run_migrations(koan_root: str):
    """Auto-migrate env vars to projects.yaml (one-shot, idempotent)."""
    from app.projects_migration import run_migration
    migration_msgs = run_migration(koan_root)
    for msg in migration_msgs:
        log("init", f"[migration] {msg}")


def run_config_migrations(koan_root: str):
    """Reshape legacy config.yaml sections in place (one-shot, idempotent)."""
    from app.config_migration import run_config_migrations as _run
    for msg in _run(koan_root):
        log("init", f"[migration] {msg}")


def ensure_projects_yaml(koan_root: str) -> list[str]:
    """Create a minimal projects.yaml if it doesn't exist.

    Seeds it with workspace/koan if present, disabling exploration
    so the agent does not autonomously explore its own repository.
    Called after migration so env-var migration takes precedence.
    """
    import yaml

    from app.projects_config import (
        resolve_projects_config_path,
        resolve_projects_config_write_path,
    )
    from app.utils import atomic_write

    # If a config already exists at either location (instance/ or repo root),
    # do nothing — never clobber or shadow the operator's persistent config.
    resolved = resolve_projects_config_path(koan_root)
    if resolved.exists():
        return []

    # Create the default at the persistent target (instance/ when present),
    # so first-time config survives managed-deployment redeploys.
    projects_yaml = resolve_projects_config_write_path(koan_root)
    projects_yaml.parent.mkdir(parents=True, exist_ok=True)

    data = {
        "defaults": {
            "git_auto_merge": {
                "enabled": False,
                "base_branch": "main",
                "strategy": "squash",
            }
        },
        "projects": {},
    }

    koan_workspace = Path(koan_root) / "workspace" / "koan"
    if koan_workspace.is_dir():
        data["projects"]["koan"] = {
            "path": str(koan_workspace),
            "exploration": False,
        }

    header = (
        "# projects.yaml — Project configuration for Kōan\n"
        "#\n"
        "# See projects.example.yaml for full documentation.\n\n"
    )
    content = header + yaml.dump(data, default_flow_style=False, sort_keys=False)
    atomic_write(projects_yaml, content)

    msgs = ["Created projects.yaml"]
    if "koan" in data["projects"]:
        msgs.append(
            "Added koan workspace project with exploration disabled"
        )
    return msgs


def populate_github_urls(koan_root: str):
    """Auto-populate github_url in projects.yaml from git remotes."""
    from app.projects_config import ensure_github_urls
    gh_msgs = ensure_github_urls(koan_root)
    for msg in gh_msgs:
        log("init", f"[github-urls] {msg}")


def detect_renamed_remotes(koan_root: str, projects: list):
    """Detect GitHub repos that were renamed and fix stale origin URLs."""
    from app.remote_rename_detector import detect_and_fix_renamed_remotes
    msgs = detect_and_fix_renamed_remotes(projects, koan_root)
    for msg in msgs:
        log("init", f"[remote-rename] {msg}")


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

    Soft validation only — detects config drift and logs warnings.
    Strict validation (validate_config_or_raise) runs separately in
    run_startup() outside _safe_run so errors are visible to the operator.
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
    from app.utils import get_file_age_seconds
    age = get_file_age_seconds(_cleanup_marker_path())
    if age is None:
        return True
    return age / 3600 >= max_age_hours


def _write_cleanup_marker():
    """Write the current timestamp to the cleanup marker file."""
    import time
    marker = _cleanup_marker_path()
    try:
        from app.utils import atomic_write
        atomic_write(marker, str(time.time()))
    except OSError:
        pass


def _load_memory_config() -> dict:
    """Load the memory: section from config.yaml with defaults."""
    try:
        from app.utils import load_config
        config = load_config()
    except Exception as e:
        import sys
        print(f"[startup_manager] load_config error: {e}", file=sys.stderr)
        config = {}
    mem_cfg = config.get("memory", {}) or {}
    return {
        "learnings_max_lines": mem_cfg.get("learnings_max_lines", 100),
        "learnings_hard_cap": mem_cfg.get("learnings_hard_cap", 200),
        "global_personality_max": mem_cfg.get("global_personality_max", 150),
        "global_emotional_max": mem_cfg.get("global_emotional_max", 100),
        "compaction_interval_hours": mem_cfg.get("compaction_interval_hours", 24),
        "log_horizon_days": mem_cfg.get("log_horizon_days", 365),
    }


def cleanup_memory(instance: str):
    """Run memory compaction and cleanup.

    Throttled based on compaction_interval_hours (default 24h) to avoid
    redundant work on fast restart cycles. On cold boot (summary.md missing
    but SNAPSHOT.md exists), hydrates memory from snapshot before running cleanup.
    """
    mem_cfg = _load_memory_config()
    interval = mem_cfg["compaction_interval_hours"]

    if not _should_run_cleanup(max_age_hours=interval):
        from app.utils import get_file_age_seconds
        age = get_file_age_seconds(_cleanup_marker_path())
        if age is not None:
            log("health", f"Memory cleanup skipped (last run {age / 3600:.0f}h ago)")
        else:
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
        restored = mgr.hydrate_from_snapshot(force=True)
        if restored:
            log("health", f"Hydrated {len(restored)} file(s) from snapshot")

    stats = mgr.run_cleanup(
        max_learnings_lines=mem_cfg["learnings_hard_cap"],
        compact_learnings_lines=mem_cfg["learnings_max_lines"],
        global_personality_max=mem_cfg["global_personality_max"],
        global_emotional_max=mem_cfg["global_emotional_max"],
        log_horizon_days=mem_cfg["log_horizon_days"],
    )
    _write_cleanup_marker()

    # Log notable compaction stats
    for key, value in stats.items():
        if key.startswith("learnings_compacted_"):
            project = key[len("learnings_compacted_"):]
            log("health", f"Learnings compacted for {project}: {value}")
        elif key.startswith("global_capped_"):
            name = key[len("global_capped_"):]
            log("health", f"Global memory capped: {name} ({value} lines removed)")


def _prune_old_missions_backups(missions_path: Path, keep: int = 5) -> None:
    """Keep only the most recent ``keep`` ``.missions.md.bak-*`` backups.

    Backups are timestamped (``YYYYMMDD-HHMMSS``) so lexical sort is
    chronological. Prevents a flapping/corrupt file from accumulating an
    unbounded set of backups in ``instance/``.
    """
    backups = sorted(missions_path.parent.glob(".missions.md.bak-*"))
    for old in backups[:-keep]:
        try:
            old.unlink()
        except OSError as exc:
            # Surface a recurring inability to prune backups instead of
            # letting them silently accumulate (the exact failure this
            # helper guards against).
            log("warn", f"Could not prune old missions backup {old.name}: {exc}")


def prune_missions_done(instance: str):
    """Validate, self-heal, and size-bound missions.md at startup.

    missions.md is the hot-path source of truth for the mission queue. In
    production it has grown gigantic and structurally corrupted (see issue
    #2085), silently degrading the agent loop. This startup task:

    1. Validates structural invariants (canonical sections present, no
       headers glued to preceding items).
    2. On corruption, backs up the original to ``.missions.md.bak-<ts>``,
       conservatively repairs it, and surfaces a loud warning.
    3. Enforces configurable size bounds (done_keep / failed_keep /
       max_lines under the ``missions:`` config section), shedding old
       completed history first.
    """
    missions_path = Path(instance) / "missions.md"
    if not missions_path.exists():
        return

    from app.config import (
        get_missions_done_keep,
        get_missions_failed_keep,
        get_missions_max_lines,
    )
    from app.missions import (
        enforce_size_bound,
        repair_missions_structure,
        validate_missions_structure,
    )
    from app.utils import atomic_write

    content = missions_path.read_text()

    # 1+2. Structural validation and conservative self-heal. A merely
    # incomplete file (a missing canonical section, e.g. fresh installs
    # without ## Failed) is healed silently; genuine corruption (headers
    # glued to items, duplicate headers, orphan items) is backed up and
    # surfaced loudly to the operator.
    issues = validate_missions_structure(content)
    if issues:
        serious = [i for i in issues if "Missing required section" not in i]
        if serious:
            ts = time.strftime("%Y%m%d-%H%M%S")
            backup = missions_path.with_name(f".missions.md.bak-{ts}")
            backup_ok = False
            try:
                backup.write_text(content)
                backup_ok = backup.exists()
            except OSError as exc:
                log("warn", f"Could not back up corrupt missions.md: {exc}")
            if not backup_ok:
                # Backup is the only safeguard before the destructive
                # atomic_write below; without it, repairing would overwrite
                # the sole copy of the data. Leave the file untouched and
                # surface a hard error instead of silently destroying it.
                log(
                    "warn",
                    "missions.md is corrupt but backup failed — leaving file "
                    "untouched, skipping repair",
                )
                return
            _prune_old_missions_backups(missions_path)
            summary = "; ".join(serious[:5])
            log("warn", f"missions.md structural corruption detected and repaired: {summary}")
            try:
                from app.notify import NotificationPriority
                from app.utils import append_to_outbox
                append_to_outbox(
                    Path(instance) / "outbox.md",
                    "⚠️ missions.md was structurally corrupt and has been auto-repaired "
                    f"(backup: {backup.name}). Issues: {summary}\n",
                    priority=NotificationPriority.WARNING,
                )
            except Exception as exc:
                log("warn", f"Failed to notify about missions.md repair: {exc}")
        content = repair_missions_structure(content)

    # 3. Enforce configurable size bounds.
    new_content, pruned = enforce_size_bound(
        content,
        max_lines=get_missions_max_lines(),
        done_keep=get_missions_done_keep(),
        failed_keep=get_missions_failed_keep(),
    )

    if issues or pruned > 0 or new_content != content:
        # S8: the store is authoritative. Reconcile it to the repaired/pruned
        # content FIRST, then regenerate the missions.md export. If the store
        # update fails we must NOT persist a diverged file (invariant 7: the
        # authoritative store never swallows-and-diverges); the exception
        # propagates to the _safe_run startup wrapper, leaving file + store both
        # at their pre-prune state.
        from app.mission_store import get_mission_store
        from app.mission_store.transition import reconcile_all
        reconcile_all(instance, new_content)
        get_mission_store(instance).mark_synced()
        atomic_write(missions_path, new_content)
        if pruned > 0:
            log("health", f"Pruned {pruned} old Done/Failed items from missions.md")


def cleanup_mission_history(instance: str):
    """Prune old entries from mission history."""
    from app.mission_history import cleanup_old_entries
    cleanup_old_entries(instance)


def check_health(koan_root: str, max_age: int = 120):
    """Check Telegram bridge health (heartbeat age)."""
    log("health", "Checking Telegram bridge health...")
    from app.health_check import check_and_alert
    check_and_alert(koan_root, max_age=max_age)


def write_config_baseline(koan_root: str):
    """Snapshot config.yaml/projects.yaml as the post-restart baseline.

    Lets the dashboard distinguish hot-reloadable (safe) edits from those
    needing a restart. Re-runs on every (re)start so "restart pending"
    clears once the agent has actually reloaded.
    """
    from app import config_sync
    config_sync.write_baseline(Path(koan_root))


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

    Skipped when:
    - KOAN_SKIP_START_PAUSE=1 (set by /resume auto-restart to avoid
      immediately re-pausing the freshly launched runner).
    - .koan-skip-start-pause file exists with a recent timestamp (set by
      /resume during startup to prevent the race where handle_start_on_pause
      re-creates the pause file after /resume removed it). Honoring a fresh
      skip file also sets KOAN_SKIP_START_PAUSE=1 so the intent survives an
      in-process auto-update restart (which re-runs this step).
    """
    if os.environ.get("KOAN_SKIP_START_PAUSE") == "1":
        log("pause", "start_on_pause skipped (KOAN_SKIP_START_PAUSE=1)")
        return

    from app.signals import SKIP_START_PAUSE_FILE

    from app.utils import get_file_age_seconds

    skip_file = Path(koan_root) / SKIP_START_PAUSE_FILE
    if skip_file.exists():
        age = get_file_age_seconds(skip_file)
        if age is not None and age < 300:  # Fresh (< 5 min) — /resume was sent during startup
            skip_file.unlink(missing_ok=True)
            # Sticky for in-process restart — skip file is one-shot but env survives.
            os.environ["KOAN_SKIP_START_PAUSE"] = "1"
            log("pause", "start_on_pause skipped (/resume requested during startup)")
            return
        skip_file.unlink(missing_ok=True)

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


def check_remote_heads(koan_root: str, instance: str, projects: list):
    """Detect remote HEAD branch changes (e.g. master → main) and update."""
    from app.head_tracker import check_all_projects, format_changes_report
    changes = check_all_projects(projects, instance, koan_root)
    if changes:
        report = format_changes_report(changes)
        log("git", report)
        from app.run import _notify
        _notify(instance, f"🔀 {report}")


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


def track_koan_commits(koan_root: str, instance: str):
    """Record Kōan's own HEAD and report changes since last startup."""
    from app.auto_update import record_and_report
    message = record_and_report(koan_root, instance)
    if message:
        from app.run import _notify_raw
        _notify_raw(instance, message)


def check_codex_cli_update(koan_root: str, instance: str) -> None:
    """Auto-update the Codex CLI when it's the active provider (once/day)."""
    from app.codex_update import check_codex_update
    check_codex_update(koan_root, instance)


def run_morning_ritual(instance: str) -> bool:
    """Execute the morning ritual. Returns True on success, False otherwise."""
    log("init", "Running morning ritual...")
    from app.rituals import run_ritual
    return run_ritual("morning", Path(instance))


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


def check_cli_binary() -> None:
    """Enter degraded (no-mission) mode if the primary CLI binary is missing.

    Runs at startup. On a missing binary this logs, sends ONE operator warning
    (⚠️, routed to whichever messaging backend is configured), and sets the
    in-memory degraded flag so the agent loop blocks mission execution while
    chat/inbox keep working. It deliberately does NOT hard-stop: the loop and
    bridge must stay alive. The PATH must be fixed and the daemon restarted to
    clear the state (see ``app.cli_health``).
    """
    from app import cli_health

    check = cli_health.check_primary_cli()
    if check.available:
        return

    msg = cli_health.warning_message(check.binary, check.provider_name)
    log("error", f"CLI binary check failed: {msg}")
    try:
        from app.notify import send_telegram
        send_telegram(msg)
    except Exception as exc:
        log("warn", f"Failed to send CLI-binary warning: {exc}")
    cli_health.set_unavailable(check.binary, check.provider_name)
    cli_health.mark_warned()


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def run_startup(koan_root: str, instance: str, projects: list):
    """Run all startup tasks (crash recovery, health, sync, etc.).

    Returns (max_runs, interval, branch_prefix) configuration tuple.
    """
    from app.banners import print_hero_banner
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
        print_hero_banner({"provider": cli_provider})
    except Exception as e:
        log("error", f"Banner display failed: {e}")

    # --- Protected startup checks ---
    # Import protected_phase lazily to avoid circular import
    # (run.py imports startup_manager, signal handling lives in run.py)
    from app.run import protected_phase

    with protected_phase("Startup checks"):
        # Reshape any legacy config.yaml sections BEFORE strict validation, so
        # an operator upgrading across a config shape change self-heals instead
        # of hitting a hard stop.
        _safe_run("Config migration", run_config_migrations, koan_root)

        # Strict config validation runs outside _safe_run so errors
        # propagate (hard stop) and reach the operator via Telegram.
        try:
            from app.config_validator import validate_config_or_raise
            validate_config_or_raise(koan_root)
        except ValueError as e:
            log("error", f"Config validation failed: {e}")
            try:
                from app.notify import send_telegram
                send_telegram(f"⚠️ Config error — agent cannot start:\n{e}")
            except Exception as exc:
                log("warn", f"Failed to send Telegram notification: {exc}")
            raise

        _safe_run("Config validation", validate_config, koan_root)
        # Verify the CLI provider binary is on PATH before any mission runs.
        # Missing → degraded (no-mission) mode + one operator warning; never
        # a hard stop, so chat/inbox keep working.
        _safe_run("CLI binary check", check_cli_binary)
        _safe_run("Crash recovery", recover_crashed_missions, instance)
        _safe_run("Running-indicator reconcile", reconcile_running_indicators, instance)
        _safe_run("Projects migration", run_migrations, koan_root)
        msgs = _safe_run("Ensure projects.yaml", ensure_projects_yaml, koan_root)
        if msgs:
            for msg in msgs:
                log("init", f"[projects] {msg}")
        try:
            from app.projects_config import resolve_projects_config_path
            resolved = resolve_projects_config_path(koan_root)
            instance_path = Path(koan_root) / "instance" / "projects.yaml"
            origin = "instance volume" if resolved == instance_path else "repo root"
            if resolved.exists():
                log("init", f"[projects] Loaded projects.yaml from {origin}: {resolved}")
            else:
                log("init", f"[projects] No projects.yaml found (looked in instance/ then repo root: {resolved})")
        except Exception as exc:
            log("warn", f"Failed to resolve projects.yaml path: {exc}")
        _safe_run("Memory JSONL migration", migrate_memory_to_jsonl, instance)
        _safe_run("Memory SQLite indexing", index_memory_sqlite, instance)
        _safe_run("GitHub URL population", populate_github_urls, koan_root)

        result = _safe_run("Workspace discovery", discover_workspace, koan_root, projects)
        if result is not None:
            projects = result

        _safe_run("Renamed remote detection", detect_renamed_remotes, koan_root, projects)

        _safe_run("Sanity checks", run_sanity_checks, instance)
        _safe_run("Memory cleanup", cleanup_memory, instance)
        _safe_run("Missions pruning", prune_missions_done, instance)
        _safe_run("Mission history cleanup", cleanup_mission_history, instance)
        # One-time ingest of missions.md into the SQLite store (idempotent;
        # gated on MissionStore.is_initialized). Analog of memory SQLite indexing.
        from app.mission_store.startup import ensure_ingested
        _safe_run("Mission store ingest", ensure_ingested, instance)
        from app.utils import reap_stale_mission_tmp_dirs
        _safe_run("Stale mission tmp sweep", reap_stale_mission_tmp_dirs)
        # A scope outlives a hard crash or SIGKILL of run.py: teardown covers
        # every ordinary mission exit, but not one that never runs. The
        # registry is exactly the durable handle for that case, and a record
        # left under this KOAN_ROOT can only be a previous incarnation of this
        # instance — the unit name is a uuid4 that cannot collide, and a
        # fallback pid-<n> record is start-time-verified before it is
        # signalled. Same shape as the stale-TMPDIR sweep above.
        from app.mission_scope import stop_registered_scopes
        _safe_run("Leaked mission scope sweep", stop_registered_scopes, koan_root)
        _safe_run("Health check", check_health, koan_root)
        _safe_run("Config baseline", write_config_baseline, koan_root)

    with protected_phase("Self-reflection check"):
        _safe_run("Self-reflection check", check_self_reflection, instance)

    # Start on pause / passive
    _safe_run("Start on pause", handle_start_on_pause, koan_root)
    _safe_run("Start passive", handle_start_passive, koan_root)

    # One-time advisory that the bridge now defaults to quiet (normal) mode.
    _safe_run("Messaging-level notice", maybe_send_messaging_level_notice, instance)

    # Load .env before git identity so KOAN_EMAIL is available.
    # run.py does not source .env itself; without this, GIT_AUTHOR_EMAIL is
    # never set and git falls back to the global ~/.gitconfig identity.
    from app.utils import load_dotenv
    load_dotenv()

    # Git identity and GitHub auth
    _safe_run("Git identity", setup_git_identity)
    _safe_run("GitHub auth", setup_github_auth)

    # Startup notification
    log("init", f"Starting. Max runs: {max_runs}, interval: {interval}s")

    # Import status/notify helpers lazily from run
    from app.run import set_status, _build_startup_status, _notify, _notify_raw

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
        _safe_run("Remote HEAD check", check_remote_heads, koan_root, instance, projects)

    # Track Kōan's own commits (after sync so HEAD is current)
    _safe_run("Commit tracker", track_koan_commits, koan_root, instance)

    # Codex CLI self-update (only when Codex is the active provider)
    _safe_run("Codex CLI update", check_codex_cli_update, koan_root, instance)

    # Auto-update check (before daily report / morning ritual)
    updated = _safe_run("Auto-update check", check_auto_update, koan_root, instance)
    if updated:
        # Restart signal has been set — notify so the human knows the agent
        # is restarting under newer code, then exit to let wrapper restart us.
        # Use _notify_raw so the verbatim text + 🔄 marker survive (skipping
        # the Claude-CLI personality reformatter).
        _notify_raw(instance, "🔄 Auto-update pulled new commits — restarting under updated code...")
        import sys
        from app.restart_manager import RESTART_EXIT_CODE
        sys.exit(RESTART_EXIT_CODE)

    # Daily report
    _safe_run("Daily report", run_daily_report)

    # Startup-status pings use _notify_raw so the 🌅/⚠️ markers and exact
    # wording reach Telegram intact (no Claude CLI rewrite). These are
    # idempotent lifecycle notices — dedup across incarnations (#2426) so a
    # restart loop doesn't re-announce the ritual on every startup.
    from app.notify_dedup import NOTICE_DEDUP_WINDOW_SECONDS
    _notify_raw(
        instance,
        "🌅 Running morning ritual (Claude CLI, up to ~90s)...",
        dedup_window=NOTICE_DEDUP_WINDOW_SECONDS,
    )
    ritual_error = ""
    with protected_phase("Morning ritual"):
        try:
            ritual_ok = run_morning_ritual(instance)
        except Exception as e:
            log("error", f"Morning ritual failed: {e}")
            ritual_ok = None
            ritual_error = str(e)
    if ritual_ok:
        _notify_raw(
            instance,
            "🌅 Morning ritual complete — preparing first iteration.",
            dedup_window=NOTICE_DEDUP_WINDOW_SECONDS,
        )
    elif ritual_ok is None:
        reason = f" ({ritual_error})" if ritual_error else ""
        _notify_raw(instance, f"⚠️ Morning ritual failed{reason} — preparing first iteration anyway.")
    else:
        _notify_raw(
            instance,
            "⏭️ Morning ritual skipped — preparing first iteration.",
            dedup_window=NOTICE_DEDUP_WINDOW_SECONDS,
        )

    # Initialize hook system and fire session_start
    from app.hooks import fire_hook, init_hooks
    _safe_run("Hook discovery", init_hooks, instance)
    _safe_run("Session start hooks", fire_hook, "session_start",
              instance_dir=instance, koan_root=koan_root)

    return max_runs, interval, branch_prefix
