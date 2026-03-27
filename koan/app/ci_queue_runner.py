"""CI queue runner — drains enqueued CI checks without blocking.

Two roles:

1. **drain_one(instance_dir)** — called from the iteration loop.  Makes a
   single non-blocking ``gh run list`` call for the oldest queue entry.
   - Pass → remove from queue.
   - Fail → inject ``/ci_check <url>`` mission and remove from queue.
   - Pending/running → skip (check again next iteration).

2. **CLI entry point** — ``python -m app.ci_queue_runner <pr-url> --project-path <path>``
   Runs the blocking CI check-and-fix for a single PR (used by the
   ``/ci_check`` fix mission path).

All status/debug output goes to stderr; stdout is reserved for JSON.
"""

import json
import sys
from pathlib import Path
from typing import Optional, Tuple


def check_ci_status(branch: str, full_repo: str) -> Tuple[str, Optional[int]]:
    """Make a single non-blocking CI status check.

    Returns:
        (status, run_id) where status is one of:
        "success", "failure", "pending", "none"
    """
    from app.github import run_gh

    try:
        raw = run_gh(
            "run", "list",
            "--branch", branch,
            "--repo", full_repo,
            "--json", "databaseId,status,conclusion",
            "--limit", "1",
        )
        runs = json.loads(raw) if raw.strip() else []
    except Exception as e:
        print(f"[ci_queue] CI status check error: {e}", file=sys.stderr)
        return ("pending", None)

    if not runs:
        return ("none", None)

    run = runs[0]
    run_id = run.get("databaseId")
    status = run.get("status", "").lower()
    conclusion = run.get("conclusion", "").lower()

    if status == "completed":
        if conclusion == "success":
            return ("success", run_id)
        return ("failure", run_id)

    # in_progress, queued, waiting, etc.
    return ("pending", run_id)


def drain_one(instance_dir: str) -> Optional[str]:
    """Check one CI queue entry (non-blocking). Returns a status message or None.

    Called once per iteration from the run loop. Checks the oldest entry,
    and based on CI status:
    - success: remove from queue, return success message
    - failure: inject /ci_check mission, remove from queue
    - pending: leave in queue (try again next iteration)
    - none: remove from queue (no CI configured)
    - expired: remove from queue (older than 24h)
    """
    from app import ci_queue

    entry = ci_queue.peek(instance_dir)
    if entry is None:
        return None

    pr_url = entry["pr_url"]
    branch = entry["branch"]
    full_repo = entry["full_repo"]
    pr_number = entry.get("pr_number", "?")

    status, run_id = check_ci_status(branch, full_repo)

    if status == "success":
        ci_queue.remove(instance_dir, pr_url)
        return f"CI passed for PR #{pr_number} ({branch})"

    if status == "failure":
        ci_queue.remove(instance_dir, pr_url)
        _inject_ci_fix_mission(instance_dir, pr_url, entry)
        return f"CI failed for PR #{pr_number} — /ci_check mission queued"

    if status == "none":
        ci_queue.remove(instance_dir, pr_url)
        return f"No CI runs found for PR #{pr_number} — removed from queue"

    # status == "pending" — leave in queue
    return None


def _inject_ci_fix_mission(instance_dir: str, pr_url: str, entry: dict):
    """Inject a /ci_check mission into the pending queue."""
    from app.missions import insert_mission
    from app.utils import modify_missions_file

    missions_path = Path(instance_dir) / "missions.md"
    project_path = entry.get("project_path", "")

    # Determine project name from path for the mission tag
    project_name = _project_name_from_path(project_path)
    tag = f"[project:{project_name}] " if project_name else ""

    mission_text = f"- {tag}/ci_check {pr_url}"

    modify_missions_file(
        missions_path,
        lambda content: insert_mission(content, mission_text, urgent=True),
    )


def _project_name_from_path(project_path: str) -> str:
    """Derive project name from its filesystem path."""
    if not project_path:
        return ""
    return Path(project_path).name


# ── CLI entry point ────────────────────────────────────────────────────
# Used by /ci_check skill dispatch: runs the blocking CI check-and-fix
# pipeline for a single PR.


def run_ci_check_and_fix(pr_url: str, project_path: str) -> Tuple[bool, str]:
    """Run the blocking CI check-and-fix for a single PR.

    This reuses the existing _run_ci_check_and_fix from rebase_pr.py
    which handles polling, Claude-based fix attempts, and re-pushing.
    """
    from app.github_url_parser import parse_pr_url

    owner, repo, pr_number = parse_pr_url(pr_url)
    full_repo = f"{owner}/{repo}"

    # Fetch minimal PR context needed for CI fix
    from app.rebase_pr import fetch_pr_context

    try:
        context = fetch_pr_context(owner, repo, pr_number)
    except Exception as e:
        return False, f"Failed to fetch PR context: {e}"

    branch = context.get("branch", "")
    base = context.get("base", "main")

    if not branch:
        return False, "Could not determine PR branch"

    from app.claude_step import _get_current_branch, _safe_checkout
    from app.rebase_pr import _run_ci_check_and_fix

    # Save current branch, checkout PR branch
    original_branch = _get_current_branch(project_path)

    try:
        from app.claude_step import _run_git
        _run_git(["git", "fetch", "origin", branch], cwd=project_path)
        _run_git(["git", "checkout", branch], cwd=project_path)
    except Exception as e:
        return False, f"Failed to checkout {branch}: {e}"

    actions_log = []

    def notify_stderr(msg):
        print(f"[ci_check] {msg}", file=sys.stderr)

    try:
        ci_section = _run_ci_check_and_fix(
            branch=branch,
            base=base,
            full_repo=full_repo,
            pr_number=pr_number,
            project_path=project_path,
            context=context,
            actions_log=actions_log,
            notify_fn=notify_stderr,
        )
    finally:
        _safe_checkout(original_branch, project_path)

    summary = "\n".join(f"- {a}" for a in actions_log)
    success = any("passed" in a.lower() for a in actions_log)

    return success, f"{ci_section}\n\nActions:\n{summary}"


def main(argv=None):
    """CLI entry point for ci_queue_runner."""
    import argparse

    from app.github_url_parser import parse_pr_url as _parse_url

    parser = argparse.ArgumentParser(
        description="Check and fix CI failures for a GitHub PR.",
    )
    parser.add_argument("url", help="GitHub PR URL")
    parser.add_argument(
        "--project-path", required=True,
        help="Local path to the project repository",
    )
    cli_args = parser.parse_args(argv)

    try:
        _parse_url(cli_args.url)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    success, summary = run_ci_check_and_fix(cli_args.url, cli_args.project_path)

    # Output JSON to stdout for mission_runner consumption
    result = {
        "success": success,
        "summary": summary,
    }
    print(json.dumps(result))

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
