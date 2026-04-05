"""
Koan -- Check runner.

Inspects a GitHub PR or issue and takes appropriate action (rebase,
review, plan). Extracted from the /check skill handler so it runs as a
queued mission via run.py instead of inline in the bridge process.

CLI:
    python3 -m app.check_runner <github-url>
    python3 -m app.check_runner https://github.com/owner/repo/pull/123
    python3 -m app.check_runner https://github.com/owner/repo/issues/42
"""

import json
from pathlib import Path
from typing import Tuple

from app.github_url_parser import search_pr_url, search_issue_url


def run_check(
    url: str,
    instance_dir: str,
    koan_root: str,
    notify_fn=None,
) -> Tuple[bool, str]:
    """Execute the check pipeline on a GitHub URL.

    Returns:
        (success, summary) tuple.
    """
    if notify_fn is None:
        from app.notify import send_telegram
        notify_fn = send_telegram

    instance_path = Path(instance_dir)

    try:
        owner, repo, pr_number = search_pr_url(url)
        return _handle_pr(
            owner, repo, pr_number, instance_path, koan_root, notify_fn,
        )
    except ValueError:
        pass

    try:
        owner, repo, issue_number = search_issue_url(url)
        return _handle_issue(
            owner, repo, issue_number, instance_path, koan_root, notify_fn,
        )
    except ValueError:
        pass

    return False, f"No valid GitHub PR or issue URL found in: {url}"


# ---------------------------------------------------------------------------
# PR handling
# ---------------------------------------------------------------------------

def _canonical_url(owner, repo, kind, number):
    """Build a canonical URL for tracker storage."""
    return f"https://github.com/{owner}/{repo}/{kind}/{number}"


def _fetch_pr_metadata(owner, repo, pr_number):
    """Fetch PR metadata via gh CLI."""
    from app.github import run_gh

    fields = (
        "state,mergeable,reviewDecision,updatedAt,"
        "headRefName,baseRefName,title,isDraft,author,url"
    )
    raw = run_gh(
        "pr", "view", pr_number,
        "--repo", f"{owner}/{repo}",
        "--json", fields,
    )
    return json.loads(raw)


def _fetch_issue_metadata(owner, repo, issue_number):
    """Fetch issue metadata via gh CLI."""
    from app.github import api

    raw = api(
        f"repos/{owner}/{repo}/issues/{issue_number}",
        jq='{"state": .state, "updatedAt": .updated_at, '
           '"title": .title, "url": .html_url, '
           '"comments": .comments}',
    )
    return json.loads(raw)


def _needs_rebase(pr_data):
    """Determine if the PR branch needs a rebase."""
    mergeable = pr_data.get("mergeable", "UNKNOWN")
    return mergeable == "CONFLICTING"


def _has_no_reviews(pr_data):
    """Return True if the PR has received no review decision yet."""
    decision = pr_data.get("reviewDecision")
    return not decision


def _handle_pr(owner, repo, pr_number, instance_dir, koan_root, notify_fn):
    """Check a pull request and decide on action."""
    from app.check_tracker import has_changed, mark_checked

    url = _canonical_url(owner, repo, "pull", pr_number)

    notify_fn(f"\U0001f50d Checking PR #{pr_number} ({owner}/{repo})...")

    try:
        pr_data = _fetch_pr_metadata(owner, repo, pr_number)
    except Exception as e:
        msg = f"\u274c Failed to fetch PR #{pr_number}: {str(e)[:300]}"
        notify_fn(msg)
        return False, msg

    updated_at = pr_data.get("updatedAt", "")
    title = pr_data.get("title", "")
    state = pr_data.get("state", "UNKNOWN")

    # Skip closed/merged PRs
    if state in ("CLOSED", "MERGED"):
        mark_checked(instance_dir, url, updated_at)
        msg = f"PR #{pr_number} is {state.lower()}. No action needed."
        notify_fn(msg)
        return True, msg

    # Check if anything changed since last check
    if not has_changed(instance_dir, url, updated_at):
        msg = (
            f"PR #{pr_number} ({title[:60]}) \u2014 no updates since last "
            "check. Skipping."
        )
        notify_fn(msg)
        return True, msg

    # Ownership check: only act on PRs from this instance
    from app.config import get_branch_prefix

    head_branch = pr_data.get("headRefName", "")
    prefix = get_branch_prefix()
    is_own = head_branch.startswith(prefix)

    if not is_own:
        mark_checked(instance_dir, url, updated_at)
        msg = (
            f"\u274c PR #{pr_number} — branch `{head_branch}` is not mine. "
            f"Skipping."
        )
        notify_fn(msg)
        return True, msg

    # Build status report
    actions = []
    missions_path = instance_dir / "missions.md"
    needs_reb = _needs_rebase(pr_data)

    # 1. Check if rebase is needed
    if needs_reb:
        _queue_rebase(owner, repo, pr_number, missions_path,
                      koan_root, instance_dir)
        actions.append("\u267b\ufe0f Rebase queued \u2014 PR has merge conflicts")

    # 2. Check if review is needed
    is_draft = pr_data.get("isDraft", False)
    if _has_no_reviews(pr_data) and not is_draft and not needs_reb:
        _queue_pr_review(owner, repo, pr_number, missions_path)
        actions.append("\U0001f4dd PR review queued \u2014 no reviews yet")

    # Record the check
    mark_checked(instance_dir, url, updated_at)

    if not actions:
        head = pr_data.get("headRefName", "?")
        base = pr_data.get("baseRefName", "?")
        mergeable = pr_data.get("mergeable", "UNKNOWN")
        review = pr_data.get("reviewDecision") or "none"
        msg = (
            f"\u2705 PR #{pr_number} ({title[:60]})\n"
            f"Branch: {head} \u2192 {base}\n"
            f"Mergeable: {mergeable} | Review: {review}\n"
            "No action needed."
        )
    else:
        summary = "\n".join(f"  \u2022 {a}" for a in actions)
        msg = f"\U0001f527 PR #{pr_number} ({title[:60]}):\n{summary}"

    notify_fn(msg)
    return True, msg


def _queue_rebase(owner, repo, pr_number, missions_path,
                  koan_root, instance_dir):
    """Queue a rebase mission for the PR."""
    from app.utils import insert_pending_mission, resolve_project_path

    project_path = resolve_project_path(repo, owner=owner)
    project_name = _resolve_project_name(repo, owner=owner)

    cmd = (
        f"cd {koan_root}/koan && "
        f"{koan_root}/.venv/bin/python3 -m app.rebase_pr "
        f"https://github.com/{owner}/{repo}/pull/{pr_number}"
    )
    if project_path:
        cmd += f" --project-path {project_path}"

    entry = (
        f"- [project:{project_name}] Rebase PR #{pr_number} "
        f"({owner}/{repo}) \u2014 run: `{cmd}`"
    )
    insert_pending_mission(missions_path, entry)


def _queue_pr_review(owner, repo, pr_number, missions_path):
    """Queue a PR review mission."""
    from app.utils import insert_pending_mission

    project_name = _resolve_project_name(repo, owner=owner)
    pr_url = f"https://github.com/{owner}/{repo}/pull/{pr_number}"

    entry = (
        f"- [project:{project_name}] Review PR #{pr_number} "
        f"({owner}/{repo}) \u2014 /pr {pr_url}"
    )
    insert_pending_mission(missions_path, entry)


# ---------------------------------------------------------------------------
# Issue handling
# ---------------------------------------------------------------------------

def _handle_issue(owner, repo, issue_number, instance_dir, koan_root, notify_fn):
    """Check an issue and trigger /plan if updated."""
    from app.check_tracker import has_changed, mark_checked

    url = _canonical_url(owner, repo, "issues", issue_number)

    notify_fn(f"\U0001f50d Checking issue #{issue_number} ({owner}/{repo})...")

    try:
        issue_data = _fetch_issue_metadata(owner, repo, issue_number)
    except Exception as e:
        msg = f"\u274c Failed to fetch issue #{issue_number}: {str(e)[:300]}"
        notify_fn(msg)
        return False, msg

    updated_at = issue_data.get("updatedAt", "")
    title = issue_data.get("title", "")
    state = issue_data.get("state", "unknown")

    # Skip closed issues
    if state == "closed":
        mark_checked(instance_dir, url, updated_at)
        msg = f"Issue #{issue_number} is closed. No action needed."
        notify_fn(msg)
        return True, msg

    # Check if anything changed since last check
    if not has_changed(instance_dir, url, updated_at):
        msg = (
            f"Issue #{issue_number} ({title[:60]}) \u2014 no updates since "
            "last check. Skipping."
        )
        notify_fn(msg)
        return True, msg

    # Queue /plan on the issue
    _queue_plan(owner, repo, issue_number, title, instance_dir, koan_root)

    mark_checked(instance_dir, url, updated_at)

    msg = (
        f"\U0001f9e0 Issue #{issue_number} ({title[:60]}) has updates.\n"
        f"  \u2022 /plan queued for iteration."
    )
    notify_fn(msg)
    return True, msg


def _queue_plan(owner, repo, issue_number, title, instance_dir, koan_root):
    """Queue a /plan mission for the issue."""
    from app.utils import insert_pending_mission
    import shlex

    project_name = _resolve_project_name(repo, owner=owner)
    issue_url = f"https://github.com/{owner}/{repo}/issues/{issue_number}"
    missions_path = instance_dir / "missions.md"

    project_path = _resolve_project_path(repo, owner=owner)
    if project_path:
        cmd = (
            f"cd {koan_root}/koan && "
            f"{koan_root}/.venv/bin/python3 -m app.plan_runner "
            f"--project-path {shlex.quote(project_path)} "
            f"--issue-url {issue_url}"
        )
        short_title = title[:80] if title else f"issue #{issue_number}"
        entry = (
            f"- [project:{project_name}] Plan iteration on {short_title} "
            f"\u2014 run: `{cmd}`"
        )
    else:
        short_title = title[:80] if title else f"issue #{issue_number}"
        entry = (
            f"- [project:{project_name}] Plan iteration on {short_title} "
            f"\u2014 /plan {issue_url}"
        )

    insert_pending_mission(missions_path, entry)


def _resolve_project_name(repo, owner=None):
    """Resolve a repo name to a known project name."""
    from app.utils import project_name_for_path, resolve_project_path

    project_path = resolve_project_path(repo, owner=owner)
    if project_path:
        return project_name_for_path(project_path)
    return repo


def _resolve_project_path(repo, owner=None):
    """Resolve a repo name to its local project path."""
    from app.utils import resolve_project_path
    return resolve_project_path(repo, owner=owner)


# ---------------------------------------------------------------------------
# CLI entry point -- python3 -m app.check_runner
# ---------------------------------------------------------------------------

def main(argv=None):
    """CLI entry point for check_runner.

    Returns exit code (0 = success, 1 = failure).
    """
    import argparse
    import os
    import sys

    parser = argparse.ArgumentParser(
        description="Check a GitHub PR or issue and take appropriate action."
    )
    parser.add_argument(
        "url",
        help="GitHub PR or issue URL to check",
    )
    parser.add_argument(
        "--instance-dir",
        default=os.environ.get("KOAN_INSTANCE_DIR", ""),
        help="Path to instance directory",
    )
    parser.add_argument(
        "--koan-root",
        default=os.environ.get("KOAN_ROOT", ""),
        help="Path to koan root directory",
    )
    cli_args = parser.parse_args(argv)

    instance_dir = cli_args.instance_dir
    koan_root = cli_args.koan_root

    if not instance_dir:
        print("Error: --instance-dir or KOAN_INSTANCE_DIR required",
              file=sys.stderr)
        return 1
    if not koan_root:
        print("Error: --koan-root or KOAN_ROOT required",
              file=sys.stderr)
        return 1

    success, summary = run_check(
        url=cli_args.url,
        instance_dir=instance_dir,
        koan_root=koan_root,
    )
    print(summary)
    return 0 if success else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
