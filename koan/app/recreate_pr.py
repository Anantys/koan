"""
Kōan -- Pull Request recreation workflow.

Recreates a PR from scratch when the original branch has diverged too far
from the target for a clean rebase.

Pipeline:
1. Fetch PR metadata + diff + comments from GitHub (understand the intent)
2. Create a fresh branch from upstream target
3. Invoke Claude to reimplement the feature (inspired by the original PR)
4. Run tests to verify
5. Push the result (force-push to original branch, or create new draft PR)
6. Comment on the original PR with cross-link
"""

import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from app.claude_step import (
    _build_pr_prompt,
    _get_current_branch,
    _get_diffstat,
    _push_with_pr_fallback,
    _run_git,
    _safe_checkout,
    run_claude_step,
    run_project_tests,
)
from app.github import run_gh
from app.prompts import load_prompt, load_skill_prompt  # noqa: F401 — safety import
from app.rebase_pr import (
    build_comment_summary,
    fetch_pr_context,
)


def run_recreate(
    owner: str,
    repo: str,
    pr_number: str,
    project_path: str,
    notify_fn=None,
    skill_dir: Optional[Path] = None,
) -> Tuple[bool, str]:
    """Execute the recreation pipeline for a pull request.

    Unlike run_rebase which preserves the branch history, this creates
    a fresh branch from the upstream target and reimplements the feature
    from scratch, using the original PR as inspiration.

    Args:
        owner: GitHub owner (e.g., "owner")
        repo: GitHub repo name (e.g., "koan")
        pr_number: PR number as string
        project_path: Local path to the project
        notify_fn: Optional callback for progress notifications.
        skill_dir: Path to the recreate skill directory for prompt resolution.

    Returns:
        (success, summary) tuple.
    """
    if notify_fn is None:
        from app.notify import send_telegram
        notify_fn = send_telegram

    full_repo = f"{owner}/{repo}"
    actions_log: List[str] = []

    # -- Step 1: Fetch PR context ------------------------------------------------
    notify_fn(f"Reading PR #{pr_number} to understand original intent...")
    try:
        context = fetch_pr_context(owner, repo, pr_number)
    except Exception as e:
        return False, f"Failed to fetch PR context: {e}"

    if not context["branch"]:
        return False, "Could not determine PR branch name."

    # Guard: reject merged or closed PRs
    pr_state = context.get("state", "").upper()
    if pr_state == "MERGED":
        return False, (
            f"PR #{pr_number} is already merged into `{context['base']}`. "
            f"/recreate is for open PRs whose branch has diverged."
        )
    if pr_state == "CLOSED":
        return False, (
            f"PR #{pr_number} is closed. "
            f"/recreate is for open PRs whose branch has diverged."
        )

    branch = context["branch"]
    base = context["base"]
    actions_log.append(f"Read PR #{pr_number}: \"{context['title']}\"")

    # Log comment summary for awareness
    comment_summary = build_comment_summary(context)
    if comment_summary and "No comments" not in comment_summary:
        actions_log.append("Read PR comments and review feedback")

    # -- Step 2: Create fresh branch from upstream target -----------------------
    notify_fn(f"Creating fresh branch from upstream `{base}`...")

    original_branch = _get_current_branch(project_path)

    # Fetch latest upstream target
    upstream_remote = _fetch_upstream_target(base, project_path)
    if not upstream_remote:
        return False, f"Could not fetch `{base}` from origin or upstream."

    # Create a fresh working branch from the upstream target
    work_branch = branch  # We'll try to reuse the original branch name
    try:
        # Delete local branch if it exists (we're recreating from scratch)
        try:
            _run_git(["git", "branch", "-D", work_branch], cwd=project_path)
        except (RuntimeError, OSError):
            pass  # Branch doesn't exist locally, that's fine

        _run_git(
            ["git", "checkout", "-b", work_branch, f"{upstream_remote}/{base}"],
            cwd=project_path,
        )
        actions_log.append(f"Created fresh branch `{work_branch}` from `{upstream_remote}/{base}`")
    except Exception as e:
        _safe_checkout(original_branch, project_path)
        return False, f"Failed to create fresh branch: {e}"

    # -- Step 3: Reimplement the feature via Claude ----------------------------
    notify_fn(f"Reimplementing feature from PR #{pr_number}...")

    reimpl_ok = _reimpl_feature(
        context, pr_number, project_path, actions_log,
        skill_dir=skill_dir,
    )

    # Claude may have created its own branch instead of staying on work_branch.
    # Detect the actual branch and use it for commit verification and push.
    current_branch = _get_current_branch(project_path)
    if current_branch != work_branch:
        actions_log.append(
            f"Claude switched to branch `{current_branch}` "
            f"(expected `{work_branch}`)"
        )
        work_branch = current_branch

    # Verify something was actually implemented
    has_changes = _has_commits_on_branch(work_branch, base, upstream_remote, project_path)
    if not has_changes:
        _safe_checkout(original_branch, project_path)
        if not reimpl_ok:
            reason = "Recreation produced no changes (reimplementation step failed)."
        else:
            reason = "Recreation produced no changes. The feature may need manual implementation."
        if actions_log:
            reason += "\n\nActions:\n" + "\n".join(f"- {a}" for a in actions_log)
        return False, reason

    # -- Step 4: Run tests ----------------------------------------------------
    notify_fn("Running tests...")
    test_result = run_project_tests(project_path)
    if test_result["passed"]:
        actions_log.append(f"Tests pass ({test_result['details']})")
    elif test_result["details"] != "command not found":
        actions_log.append(f"Tests: {test_result['details']} (non-blocking)")

    # -- Step 4b: Collect diffstat before push ---------------------------------
    diffstat = _get_diffstat(f"{upstream_remote}/{base}", project_path)

    # -- Step 5: Push the result -----------------------------------------------
    notify_fn(f"Pushing `{work_branch}`...")
    push_result = _push_recreated(
        work_branch, base, full_repo, pr_number, context, project_path
    )
    actions_log.extend(push_result["actions"])

    if not push_result["success"]:
        _safe_checkout(original_branch, project_path)
        return False, (
            f"Push failed: {push_result.get('error', 'unknown')}\n\n"
            "Actions completed:\n" +
            "\n".join(f"- {a}" for a in actions_log)
        )

    # -- Step 6: Comment on the original PR ------------------------------------
    comment_body = _build_recreate_comment(
        pr_number, work_branch, base, actions_log, context,
        new_pr_url=push_result.get("new_pr_url"),
        diffstat=diffstat,
    )

    try:
        run_gh(
            "pr", "comment", pr_number,
            "--repo", full_repo,
            "--body", comment_body,
        )
        actions_log.append("Commented on original PR")
    except Exception as e:
        # Non-fatal
        actions_log.append(f"Comment failed (non-fatal): {str(e)[:100]}")

    # Restore original branch
    _safe_checkout(original_branch, project_path)

    summary = f"PR #{pr_number} recreated.\n" + "\n".join(
        f"- {a}" for a in actions_log
    )
    return True, summary


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fetch_upstream_target(base: str, project_path: str) -> Optional[str]:
    """Fetch the target branch from upstream or origin.

    Prefers upstream (source-of-truth in fork setups) over origin
    to ensure the freshest base when recreating a PR from scratch.

    Returns the remote name used, or None on failure.
    """
    for remote in ("upstream", "origin"):
        try:
            _run_git(["git", "fetch", remote, base], cwd=project_path)
            return remote
        except (RuntimeError, OSError):
            continue
    return None


def _build_recreate_prompt(context: dict, skill_dir: Optional[Path] = None) -> str:
    """Build a prompt for Claude to reimplement the feature from scratch."""
    return _build_pr_prompt("recreate", context, skill_dir=skill_dir)


def _reimpl_feature(
    context: dict,
    pr_number: str,
    project_path: str,
    actions_log: List[str],
    skill_dir: Optional[Path] = None,
) -> bool:
    """Reimplement the feature via Claude, inspired by the original PR.

    Returns True if the step produced a commit, False otherwise.
    """
    from app.config import get_skill_timeout
    prompt = _build_recreate_prompt(context, skill_dir=skill_dir)
    return run_claude_step(
        prompt=prompt,
        project_path=project_path,
        commit_msg=f"feat: recreate PR #{pr_number} — {context.get('title', 'reimplementation')}",
        success_label="Reimplemented feature from scratch",
        failure_label="Feature reimplementation step failed",
        actions_log=actions_log,
        max_turns=30,
        timeout=get_skill_timeout(),
    )


def _has_commits_on_branch(
    branch: str, base: str, remote: str, project_path: str
) -> bool:
    """Check if the branch has commits beyond the upstream target.

    Falls back to checking HEAD if the named branch ref fails (e.g. when
    the branch name is ambiguous due to dots/slashes).
    """
    for ref in (branch, "HEAD"):
        try:
            log = _run_git(
                ["git", "log", f"{remote}/{base}..{ref}", "--oneline"],
                cwd=project_path,
            )
            if log.strip():
                return True
        except (RuntimeError, OSError):
            continue
    return False


def _push_recreated(
    branch: str,
    base: str,
    full_repo: str,
    pr_number: str,
    context: dict,
    project_path: str,
) -> dict:
    """Push recreated branch, falling back to new draft PR if permission denied."""
    return _push_with_pr_fallback(
        branch, base, full_repo, pr_number, context, project_path,
        pr_type="recreate",
    )


def _build_recreate_comment(
    pr_number: str,
    branch: str,
    base: str,
    actions_log: List[str],
    context: dict,
    new_pr_url: Optional[str] = None,
    diffstat: str = "",
) -> str:
    """Build a markdown comment summarizing the recreation."""
    title = context.get("title", f"PR #{pr_number}")

    # Filter out mechanical steps for cleaner output
    meaningful_actions = [
        a for a in actions_log
        if not a.startswith(f"Read PR #{pr_number}:")
        and not a.startswith("Read PR comments")
        and not a.startswith("Commented on")
    ]
    actions_md = "\n".join(
        f"- {a}" for a in meaningful_actions
    ) if meaningful_actions else "- Reimplemented from scratch"

    parts = [f"## Recreated: {title}\n"]
    parts.append(
        f"Branch `{branch}` diverged too far from `{base}` for a clean rebase — "
        f"reimplemented from scratch.\n"
    )

    if new_pr_url:
        parts.append(f"**New PR**: {new_pr_url}\n")
    else:
        parts.append(f"Branch `{branch}` force-pushed with the recreation.\n")

    if diffstat:
        parts.append(f"**Diff**: {diffstat}\n")

    # Extract test result from actions for prominent display
    test_actions = [a for a in actions_log if a.startswith("Tests")]
    if test_actions:
        parts.append(f"**{test_actions[0]}**\n")
        # Remove from actions_md to avoid duplication
        meaningful_actions = [
            a for a in meaningful_actions if not a.startswith("Tests")
        ]
        actions_md = "\n".join(
            f"- {a}" for a in meaningful_actions
        ) if meaningful_actions else "- Reimplemented from scratch"

    parts.append(f"### Actions\n\n{actions_md}\n")
    parts.append("---\n_Automated by Kōan_")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# CLI entry point -- python3 -m app.recreate_pr <url> --project-path <path>
# ---------------------------------------------------------------------------

def main(argv=None):
    """CLI entry point for recreate_pr.

    Returns exit code (0 = success, 1 = failure).
    """
    import argparse
    import sys

    from app.github_url_parser import parse_pr_url as _parse_url

    parser = argparse.ArgumentParser(
        description="Recreate a GitHub PR from scratch on current upstream."
    )
    parser.add_argument("url", help="GitHub PR URL")
    parser.add_argument(
        "--project-path", required=True,
        help="Local path to the project repository",
    )
    cli_args = parser.parse_args(argv)

    try:
        owner, repo, pr_number = _parse_url(cli_args.url)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    skill_dir = Path(__file__).resolve().parent.parent / "skills" / "core" / "recreate"

    success, summary = run_recreate(
        owner, repo, pr_number, cli_args.project_path,
        skill_dir=skill_dir,
    )
    print(summary)
    return 0 if success else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
