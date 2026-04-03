"""CI queue runner — drains enqueued CI checks without blocking.

Two roles:

1. **drain_one(instance_dir)** — called from the iteration loop.  Reads the
   ## CI section from missions.md and checks each entry non-blocking.
   - Pass → remove from ## CI, write outbox success message.
   - Fail → increment attempt counter, inject ``/ci_check <url>`` mission.
            If max attempts reached, remove from ## CI, write outbox failure.
   - Pending/running → skip (check again next iteration).
   - None → remove from ## CI (no CI configured).

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
    """Check CI entries in ## CI section (non-blocking). Returns a status message or None.

    Called once per iteration from the run loop. Reads the ## CI section,
    picks the first (oldest) entry, and based on CI status:
    - success: remove from ## CI, send outbox notification
    - failure (under max): increment attempt, inject /ci_check mission
    - failure (at max): remove from ## CI, send failure outbox notification
    - pending: leave in ## CI (try again next iteration)
    - none: remove from ## CI (no CI configured)

    Also migrates legacy .ci-queue.json entries to ## CI on first call.
    """
    from app.missions import get_ci_items
    from app.utils import modify_missions_file

    missions_path = Path(instance_dir) / "missions.md"

    # One-time migration from legacy JSON queue
    _maybe_migrate_json_queue(instance_dir, missions_path)

    content = missions_path.read_text() if missions_path.exists() else ""
    items = get_ci_items(content)
    if not items:
        return None

    # Process first (oldest) entry
    entry = items[0]
    pr_url = entry["pr_url"]
    branch = entry["branch"]
    full_repo = entry["full_repo"]
    pr_number = entry.get("pr_number", "?")
    attempt = entry["attempt"]
    max_attempts = entry["max_attempts"]

    status, _run_id = check_ci_status(branch, full_repo)

    if status == "success":
        modify_missions_file(
            missions_path,
            lambda c: __import__("app.missions", fromlist=["remove_ci_item"]).remove_ci_item(c, pr_url),
        )
        _write_outbox(
            instance_dir,
            f"✅ CI passed for PR #{pr_number} — ready for review: {pr_url}",
        )
        return f"CI passed for PR #{pr_number} ({branch})"

    if status == "failure":
        if attempt < max_attempts:
            # Increment attempt counter, inject fix mission
            modify_missions_file(
                missions_path,
                lambda c: __import__("app.missions", fromlist=["update_ci_item_attempt"]).update_ci_item_attempt(c, pr_url),
            )
            _inject_ci_fix_mission(instance_dir, pr_url, entry)
            return f"CI failed for PR #{pr_number} — /ci_check mission queued (attempt {attempt + 1}/{max_attempts})"
        else:
            # Max attempts exhausted
            modify_missions_file(
                missions_path,
                lambda c: __import__("app.missions", fromlist=["remove_ci_item"]).remove_ci_item(c, pr_url),
            )
            _write_outbox(
                instance_dir,
                f"❌ CI still failing after {max_attempts} attempts for PR #{pr_number}: {pr_url}",
            )
            return f"CI failed {max_attempts} times for PR #{pr_number} — giving up"

    if status == "none":
        modify_missions_file(
            missions_path,
            lambda c: __import__("app.missions", fromlist=["remove_ci_item"]).remove_ci_item(c, pr_url),
        )
        return f"No CI runs found for PR #{pr_number} — removed from ## CI"

    # status == "pending" — leave in ## CI
    return None


def _inject_ci_fix_mission(instance_dir: str, pr_url: str, entry: dict):
    """Inject a /ci_check mission into the pending queue."""
    from app.missions import insert_mission
    from app.utils import modify_missions_file

    missions_path = Path(instance_dir) / "missions.md"
    project_name = entry.get("project") or _project_name_from_path(
        entry.get("project_path", "")
    )
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


def _write_outbox(instance_dir: str, message: str):
    """Append a message to outbox.md."""
    from app.utils import append_to_outbox

    outbox_path = Path(instance_dir) / "outbox.md"
    try:
        append_to_outbox(outbox_path, message)
    except Exception as e:
        print(f"[ci_queue] Failed to write outbox: {e}", file=sys.stderr)


def _maybe_migrate_json_queue(instance_dir: str, missions_path: Path):
    """One-time migration from .ci-queue.json to ## CI section in missions.md.

    Reads any entries from the legacy JSON queue and adds them to ## CI,
    then removes the JSON file. Migrated entries start at attempt 0.
    """
    import os

    json_path = Path(instance_dir) / ".ci-queue.json"
    if not json_path.exists():
        return

    try:
        import json as _json
        data = _json.loads(json_path.read_text())
        entries = data if isinstance(data, list) else []
    except Exception as e:
        print(f"[ci_queue] Failed to read legacy JSON queue: {e}", file=sys.stderr)
        entries = []

    if not entries:
        try:
            os.remove(json_path)
        except OSError:
            pass
        return

    from app.missions import add_ci_item
    from app.utils import load_config, modify_missions_file

    config = load_config()
    max_attempts = config.get("ci_fix_max_attempts", 5)

    for entry in entries:
        pr_url = entry.get("pr_url", "")
        branch = entry.get("branch", "")
        full_repo = entry.get("full_repo", "")
        pr_number = entry.get("pr_number", "")
        project_path = entry.get("project_path", "")
        project_name = _project_name_from_path(project_path)

        if not pr_url or not branch or not full_repo:
            continue

        modify_missions_file(
            missions_path,
            lambda c, _pn=project_name, _url=pr_url, _num=pr_number, _b=branch, _r=full_repo, _m=max_attempts: add_ci_item(
                c, _pn, _url, _num, _b, _r, _m
            ),
        )
        print(f"[ci_queue] Migrated {pr_url} from JSON queue to ## CI", file=sys.stderr)

    try:
        os.remove(json_path)
        lock_path = Path(instance_dir) / ".ci-queue.lock"
        if lock_path.exists():
            os.remove(lock_path)
    except OSError:
        pass


def _reenqueue_for_monitoring(
    pr_url: str, branch: str, full_repo: str,
    pr_number: str, project_path: str,
):
    """Re-enqueue a PR for CI monitoring in the ## CI section after pushing a fix.

    This ensures drain_one() picks up the new CI run result during
    interruptible_sleep, rather than leaving it unmonitored.
    """
    import os

    koan_root = os.environ.get("KOAN_ROOT", "")
    if not koan_root:
        print("[ci_check] KOAN_ROOT not set, cannot re-enqueue", file=sys.stderr)
        return

    instance_dir = os.path.join(koan_root, "instance")
    missions_path = Path(instance_dir) / "missions.md"
    project_name = _project_name_from_path(project_path)

    from app.missions import add_ci_item
    from app.utils import load_config, modify_missions_file

    config = load_config()
    max_attempts = config.get("ci_fix_max_attempts", 5)

    try:
        modify_missions_file(
            missions_path,
            lambda c: add_ci_item(c, project_name, pr_url, pr_number, branch, full_repo, max_attempts),
        )
        print(f"[ci_check] Re-enqueued {pr_url} for CI monitoring in ## CI", file=sys.stderr)
    except Exception as e:
        print(f"[ci_check] Failed to re-enqueue: {e}", file=sys.stderr)


# ── CLI entry point ────────────────────────────────────────────────────
# Used by /ci_check skill dispatch: runs the blocking CI check-and-fix
# pipeline for a single PR.


def run_ci_check_and_fix(pr_url: str, project_path: str) -> Tuple[bool, str]:
    """Run the CI check-and-fix pipeline for a single PR.

    Unlike the rebase path (which polls CI for up to 10 minutes), this
    uses a non-blocking status check — drain_one() has already confirmed
    CI failed before injecting this mission, so we skip redundant polling.

    Steps:
    1. Fetch PR context and confirm CI failure (non-blocking)
    2. Checkout the PR branch
    3. Attempt Claude-based fix (up to max_attempts from ## CI entry)
    4. Force-push fixes and re-check CI
    5. Restore original branch
    """
    import os

    from app.github_url_parser import parse_pr_url

    owner, repo, pr_number = parse_pr_url(pr_url)
    full_repo = f"{owner}/{repo}"

    # Determine max attempts from ## CI entry (respects per-enqueue config)
    max_fix_attempts = 2  # fallback if not in ## CI
    koan_root = os.environ.get("KOAN_ROOT", "")
    if koan_root:
        missions_path = Path(koan_root) / "instance" / "missions.md"
        if missions_path.exists():
            from app.missions import get_ci_items
            items = get_ci_items(missions_path.read_text())
            for item in items:
                if item["pr_url"] == pr_url:
                    max_fix_attempts = item["max_attempts"]
                    break

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

    # Non-blocking CI status check — skip the 10-minute polling loop.
    # drain_one() already confirmed failure, but we need the run_id for logs.
    status, run_id = check_ci_status(branch, full_repo)
    print(f"[ci_check] CI status for {branch}: {status}", file=sys.stderr)

    if status == "success":
        return True, "CI already passing — no fix needed."

    if status == "pending":
        # CI still running — don't attempt fixes against stale logs.
        # drain_one will re-check on the next iteration when CI completes.
        return False, "CI still pending — will retry when CI completes."

    if status not in ("failure",):
        return False, f"CI status is '{status}' — nothing to fix."

    # Fetch failure logs (non-blocking)
    ci_logs = ""
    if run_id:
        from app.claude_step import _fetch_failed_logs
        ci_logs = _fetch_failed_logs(run_id, full_repo)

    if not ci_logs:
        return False, "CI failed but no failure logs available."

    # Check PR state before attempting fix
    from app.rebase_pr import _check_pr_state
    pr_state, mergeable = _check_pr_state(pr_number, full_repo)

    if pr_state == "MERGED":
        return True, "PR already merged — CI fix skipped."

    if mergeable == "CONFLICTING":
        return False, "PR has merge conflicts — CI fix skipped (rebase needed first)."

    # Checkout the PR branch
    from app.claude_step import _get_current_branch, _run_git, _safe_checkout

    original_branch = _get_current_branch(project_path)

    try:
        _run_git(
            ["git", "fetch", "origin", f"{branch}:{branch}"],
            cwd=project_path,
        )
        _run_git(["git", "checkout", branch], cwd=project_path)
    except Exception as e:
        return False, f"Failed to checkout {branch}: {e}"

    actions_log = []

    try:
        success = _attempt_ci_fixes(
            branch=branch,
            base=base,
            full_repo=full_repo,
            pr_number=pr_number,
            pr_url=pr_url,
            project_path=project_path,
            context=context,
            ci_logs=ci_logs,
            actions_log=actions_log,
            max_attempts=max_fix_attempts,
        )
    except Exception as e:
        actions_log.append(f"CI check/fix crashed: {e}")
        success = False
    finally:
        _safe_checkout(original_branch, project_path)

    summary = "\n".join(f"- {a}" for a in actions_log)
    return success, f"Actions:\n{summary}"


def _attempt_ci_fixes(
    branch: str,
    base: str,
    full_repo: str,
    pr_number: str,
    pr_url: str,
    project_path: str,
    context: dict,
    ci_logs: str,
    actions_log: list,
    max_attempts: int,
) -> bool:
    """Attempt to fix CI failures using Claude. Returns True if CI passes."""
    from app.claude_step import (
        _fetch_failed_logs,
        _run_git,
        run_claude_step,
    )
    from app.rebase_pr import (
        _build_ci_fix_prompt,
        _force_push,
        truncate_text,
    )

    for attempt in range(1, max_attempts + 1):
        print(f"[ci_check] Fix attempt {attempt}/{max_attempts}", file=sys.stderr)
        actions_log.append(f"CI fix attempt {attempt}/{max_attempts}")

        # Get the current diff for context
        diff = ""
        try:
            diff = _run_git(
                ["git", "diff", f"origin/{base}..HEAD"],
                cwd=project_path, timeout=30,
            )
        except Exception as e:
            print(f"[ci_check] diff fetch failed: {e}", file=sys.stderr)
        diff = truncate_text(diff, 8000)

        # Build prompt and run Claude
        ci_fix_prompt = _build_ci_fix_prompt(context, ci_logs, diff)

        fixed = run_claude_step(
            prompt=ci_fix_prompt,
            project_path=project_path,
            commit_msg=f"fix: resolve CI failures on #{pr_number} (attempt {attempt})",
            success_label=f"Applied CI fix (attempt {attempt})",
            failure_label=f"CI fix step failed (attempt {attempt})",
            actions_log=actions_log,
            max_turns=15,
        )

        if not fixed:
            actions_log.append("Claude produced no changes — giving up")
            break

        # Force-push the fix
        try:
            _force_push("origin", branch, project_path)
        except Exception as e:
            actions_log.append(f"Push failed: {str(e)[:100]}")
            break

        actions_log.append(f"Pushed CI fix (attempt {attempt})")

        # Re-check CI (non-blocking — just check if the new run started)
        import time
        time.sleep(15)  # Brief wait for GitHub to register the push
        new_status, new_run_id = check_ci_status(branch, full_repo)

        if new_status == "success":
            actions_log.append(f"CI passed after fix attempt {attempt}")
            return True

        if new_status == "pending":
            # CI is running with our fix — re-enqueue so drain_one monitors
            # the result during interruptible_sleep (~30s checks).
            _reenqueue_for_monitoring(pr_url, branch, full_repo, pr_number, project_path)
            actions_log.append(f"CI running after fix push (attempt {attempt}) — re-enqueued for monitoring")
            return True

        # CI already shows failure (unlikely this fast) — get new logs
        if new_run_id:
            ci_logs = _fetch_failed_logs(new_run_id, full_repo)

    actions_log.append(f"CI still failing after {max_attempts} fix attempts")
    return False


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

    try:
        success, summary = run_ci_check_and_fix(cli_args.url, cli_args.project_path)
    except Exception as exc:
        print(f"[ci_check] Unexpected error: {exc}", file=sys.stderr)
        success = False
        summary = f"CI check crashed: {exc}"

    # Output JSON to stdout for mission_runner consumption
    result = {
        "success": success,
        "summary": summary,
    }
    print(json.dumps(result))

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
