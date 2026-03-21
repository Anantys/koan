"""Automated CI failure recovery for Kōan-created PRs.

When a Kōan PR fails CI, this module orchestrates the recovery:
1. Check if auto-recovery is enabled and retries remain.
2. Fetch CI failure logs from GitHub.
3. Dispatch a fix mission to missions.md.
4. Track attempt count and cooldown in check_tracker.
5. Escalate to human via outbox.md after max retries.
"""

from datetime import datetime, timezone
from pathlib import Path


def handle_ci_failure(
    instance_dir,
    pr_url,
    pr_number,
    project_name,
    repo,
    config,
):
    """Respond to a CI failure on a Kōan-created PR.

    Args:
        instance_dir: Path to instance directory.
        pr_url: Canonical GitHub PR URL.
        pr_number: PR number (string or int).
        project_name: Resolved project name.
        repo: Repository in "owner/repo" format.
        config: Loaded projects config dict (from load_projects_config).

    Returns:
        One of: "dispatched", "escalated", "skipped_disabled",
                "skipped_max_retries", "skipped_cooldown".
    """
    from app.check_tracker import get_ci_status, get_ci_attempt_count, set_ci_status
    from app.projects_config import get_ci_recovery_config

    cfg = get_ci_recovery_config(config, project_name)

    if not cfg["auto"]:
        return "skipped_disabled"

    attempt_count = get_ci_attempt_count(instance_dir, pr_url)

    if attempt_count >= cfg["retries"]:
        _write_escalation(instance_dir, pr_url, attempt_count, cfg["retries"])
        return "escalated"

    # Check cooldown
    ci_status = get_ci_status(instance_dir, pr_url)
    if ci_status and ci_status.get("last_attempt_at"):
        last_attempt = _parse_iso(ci_status["last_attempt_at"])
        if last_attempt:
            elapsed_minutes = (
                datetime.now(timezone.utc) - last_attempt
            ).total_seconds() / 60
            if elapsed_minutes < cfg["cooldown_minutes"]:
                return "skipped_cooldown"

    # Check if mission already queued for this PR
    if _mission_already_queued(instance_dir, pr_number):
        return "skipped_cooldown"

    # Fetch CI logs
    error_summary = _fetch_logs(pr_number, repo)

    # Dispatch fix mission
    _dispatch_mission(
        instance_dir, pr_url, pr_number, project_name, error_summary
    )

    # Record attempt
    set_ci_status(instance_dir, pr_url, "fix_dispatched", attempt_count + 1)

    return "dispatched"


def format_escalation_message(pr_url, attempt_count, max_retries):
    """Format a human-readable escalation message for outbox.md."""
    return (
        f"\u26a0\ufe0f CI recovery escalation: PR {pr_url} has failed CI "
        f"{attempt_count} time(s) (max {max_retries}). "
        "Manual intervention required."
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fetch_logs(pr_number, repo):
    """Fetch and aggregate CI failure logs."""
    from app.github import get_failed_check_runs, fetch_ci_failure_logs

    failed_runs = get_failed_check_runs(pr_number, repo)
    if not failed_runs:
        return "(no CI log available)"

    parts = []
    for run in failed_runs:
        run_id = run.get("databaseId")
        name = run.get("name", "unknown")
        if run_id:
            log = fetch_ci_failure_logs(repo, run_id)
            if log:
                parts.append(f"### {name}\n{log}")

    return "\n\n".join(parts) if parts else "(no CI log available)"


def _dispatch_mission(instance_dir, pr_url, pr_number, project_name, error_summary):
    """Append a CI fix mission to missions.md."""
    from app.prompts import load_prompt
    from app.utils import insert_pending_mission

    prompt = load_prompt(
        "ci-fix-mission",
        pr_url=pr_url,
        pr_number=str(pr_number),
        project_name=project_name,
        error_summary=error_summary,
    )
    # Build a single-line mission entry pointing to the full prompt
    entry = (
        f"- [project:{project_name}] Fix CI failure on PR #{pr_number} "
        f"({pr_url}) \u2014 {prompt}"
    )
    missions_path = Path(instance_dir) / "missions.md"
    insert_pending_mission(missions_path, entry)


def _write_escalation(instance_dir, pr_url, attempt_count, max_retries):
    """Write an escalation message to outbox.md."""
    from app.utils import atomic_write

    outbox_path = Path(instance_dir) / "outbox.md"
    msg = format_escalation_message(pr_url, attempt_count, max_retries)

    # Append to outbox (read existing + append)
    existing = ""
    if outbox_path.exists():
        try:
            existing = outbox_path.read_text()
        except OSError:
            existing = ""

    atomic_write(outbox_path, existing + msg + "\n")


def _mission_already_queued(instance_dir, pr_number):
    """Return True if a CI fix mission for this PR is already in missions.md."""
    missions_path = Path(instance_dir) / "missions.md"
    if not missions_path.exists():
        return False
    try:
        content = missions_path.read_text()
        return f"Fix CI failure on PR #{pr_number}" in content
    except OSError:
        return False


def _parse_iso(ts):
    """Parse an ISO-8601 timestamp string, returning a datetime or None."""
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
