"""
Kōan -- Pull Request rebase workflow.

Rebases a PR branch onto its target branch, analyzing review comments
and applying requested changes via Claude before pushing.

Pipeline:
1. Fetch PR metadata + comments from GitHub
2. Checkout the PR branch locally
3. Rebase onto the upstream target branch (resolving conflicts via Claude if needed)
4. Analyze review comments and apply changes (Claude-powered, if feedback exists)
5. Force-push to the existing branch (never creates a new PR)
6. Comment on the PR with a summary
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from app.claude_step import (
    _build_pr_prompt,
    _get_current_branch,
    _get_diffstat,
    _run_git,
    _safe_checkout,
    run_claude,
)
from app.github import run_gh
from app.prompts import load_prompt, load_prompt_or_skill, load_skill_prompt  # noqa: F401 — safety import
from app.utils import truncate_text


def fetch_pr_context(owner: str, repo: str, pr_number: str) -> dict:
    """Fetch PR details, diff, and all comments via gh CLI.

    Returns a dict with keys: title, body, branch, base, state, author, url,
    diff, review_comments, reviews, issue_comments.
    """
    full_repo = f"{owner}/{repo}"

    # Fetch PR metadata
    pr_json = run_gh(
        "pr", "view", pr_number, "--repo", full_repo, "--json",
        "title,body,headRefName,baseRefName,state,author,url",
    )

    # Fetch PR diff (may fail for very large PRs — GitHub HTTP 406)
    try:
        diff = run_gh("pr", "diff", pr_number, "--repo", full_repo)
    except RuntimeError:
        diff = ""

    # Fetch review comments (inline code comments)
    try:
        comments_json = run_gh(
            "api", f"repos/{full_repo}/pulls/{pr_number}/comments",
            "--paginate", "--jq",
            r'.[] | "[\(.path):\(.line // .original_line)] @\(.user.login): \(.body)"',
        )
    except RuntimeError:
        comments_json = ""

    # Fetch PR-level review comments (top-level reviews)
    try:
        reviews_json = run_gh(
            "api", f"repos/{full_repo}/pulls/{pr_number}/reviews",
            "--paginate", "--jq",
            r'.[] | select(.body != "") | "@\(.user.login) (\(.state)): \(.body)"',
        )
    except RuntimeError:
        reviews_json = ""

    # Fetch issue-level comments (conversation thread)
    try:
        issue_comments = run_gh(
            "api", f"repos/{full_repo}/issues/{pr_number}/comments",
            "--paginate", "--jq",
            r'.[] | "@\(.user.login): \(.body)"',
        )
    except RuntimeError:
        issue_comments = ""

    try:
        metadata = json.loads(pr_json)
    except (json.JSONDecodeError, TypeError):
        metadata = {}

    return {
        "title": metadata.get("title", ""),
        "body": metadata.get("body", ""),
        "branch": metadata.get("headRefName", ""),
        "base": metadata.get("baseRefName", "main"),
        "state": metadata.get("state", ""),
        "author": metadata.get("author", {}).get("login", ""),
        "url": metadata.get("url", ""),
        "diff": truncate_text(diff, 8000),
        "review_comments": truncate_text(comments_json, 4000),
        "reviews": truncate_text(reviews_json, 3000),
        "issue_comments": truncate_text(issue_comments, 3000),
    }


def build_comment_summary(context: dict) -> str:
    """Build a human-readable summary of all PR feedback.

    Useful for understanding what reviewers asked for before rebasing.
    """
    parts = []

    if context.get("reviews"):
        parts.append("### Reviews\n" + context["reviews"])
    if context.get("review_comments"):
        parts.append("### Inline Comments\n" + context["review_comments"])
    if context.get("issue_comments"):
        parts.append("### Discussion\n" + context["issue_comments"])

    if not parts:
        return "No comments or reviews found on this PR."

    return "\n\n".join(parts)


def run_rebase(
    owner: str,
    repo: str,
    pr_number: str,
    project_path: str,
    notify_fn=None,
    skill_dir: Optional[Path] = None,
) -> Tuple[bool, str]:
    """Execute the rebase pipeline for a pull request.

    Steps:
        1. Fetch PR context from GitHub (metadata + all comments)
        2. Checkout the PR branch locally
        3. Rebase onto the upstream target branch
        4. Analyze review comments and apply changes (if feedback exists)
        5. Force-push to the existing branch (always recycles the PR)
        6. Comment on the PR with a summary

    Args:
        owner: GitHub owner (e.g., "owner")
        repo: GitHub repo name (e.g., "koan")
        pr_number: PR number as string
        project_path: Local path to the project
        notify_fn: Optional callback for progress notifications.
        skill_dir: Path to the rebase skill directory for prompt resolution.

    Returns:
        (success, summary) tuple.
    """
    if notify_fn is None:
        from app.notify import send_telegram
        notify_fn = send_telegram

    full_repo = f"{owner}/{repo}"
    actions_log: List[str] = []

    # ── Step 1: Fetch PR context ──────────────────────────────────────
    notify_fn(f"Reading PR #{pr_number}...")
    try:
        context = fetch_pr_context(owner, repo, pr_number)
    except Exception as e:
        return False, f"Failed to fetch PR context: {e}"

    if not context["branch"]:
        return False, "Could not determine PR branch name."

    branch = context["branch"]
    base = context["base"]

    # Log comment summary for awareness
    comment_summary = build_comment_summary(context)
    if comment_summary and "No comments" not in comment_summary:
        actions_log.append("Read PR comments and review feedback")

    # ── Step 2: Checkout the PR branch ────────────────────────────────
    notify_fn(f"Checking out `{branch}`...")

    # Save current branch to restore later
    original_branch = _get_current_branch(project_path)

    try:
        _checkout_pr_branch(branch, project_path)
    except Exception as e:
        return False, f"Failed to checkout branch `{branch}`: {e}"

    # ── Step 3: Rebase onto target branch ─────────────────────────────
    notify_fn(f"Rebasing `{branch}` onto `{base}`...")
    rebase_remote = _rebase_with_conflict_resolution(
        base, project_path, context, actions_log,
        notify_fn=notify_fn, skill_dir=skill_dir,
    )
    if rebase_remote:
        actions_log.append(f"Rebased `{branch}` onto `{rebase_remote}/{base}`")
    else:
        _safe_checkout(original_branch, project_path)
        return False, f"Rebase failed on `{base}` (tried origin and upstream). Could not resolve conflicts."

    # ── Step 4: Analyze review comments and apply changes ──────────────
    has_review_feedback = bool(
        context.get("review_comments", "").strip()
        or context.get("reviews", "").strip()
        or context.get("issue_comments", "").strip()
    )

    if has_review_feedback:
        notify_fn(f"Analyzing review comments on `{branch}`...")
        _apply_review_feedback(
            context, pr_number, project_path, actions_log,
            skill_dir=skill_dir,
        )

        # Claude may switch branches during feedback — ensure we're still
        # on the expected branch before pushing.
        current = _get_current_branch(project_path)
        if current != branch:
            actions_log.append(
                f"Note: Claude switched to `{current}`, "
                f"restoring `{branch}`"
            )
            _safe_checkout(branch, project_path)

    # ── Step 5: Collect diffstat before push ──────────────────────────
    diffstat = _get_diffstat(f"{rebase_remote}/{base}", project_path)

    # ── Step 6: Push the result ───────────────────────────────────────
    notify_fn(f"Pushing `{branch}`...")
    push_result = _push_with_fallback(
        branch, base, full_repo, pr_number, context, project_path
    )
    actions_log.extend(push_result["actions"])

    if not push_result["success"]:
        _safe_checkout(original_branch, project_path)
        return False, (
            f"Push failed: {push_result.get('error', 'unknown')}\n\n"
            f"Actions completed:\n" +
            "\n".join(f"- {a}" for a in actions_log)
        )

    # ── Step 7: Comment on the PR ─────────────────────────────────────
    comment_body = _build_rebase_comment(
        pr_number, branch, base, actions_log, context,
        diffstat=diffstat,
    )

    try:
        run_gh(
            "pr", "comment", pr_number,
            "--repo", full_repo,
            "--body", comment_body,
        )
        actions_log.append("Commented on PR")
    except Exception as e:
        # Non-fatal — the rebase itself succeeded
        actions_log.append(f"Comment failed (non-fatal): {str(e)[:100]}")

    # Restore original branch
    _safe_checkout(original_branch, project_path)

    summary = f"PR #{pr_number} rebased.\n" + "\n".join(
        f"- {a}" for a in actions_log
    )
    return True, summary


# ---------------------------------------------------------------------------
# Conflict-aware rebase
# ---------------------------------------------------------------------------

def _rebase_with_conflict_resolution(
    base: str,
    project_path: str,
    context: dict,
    actions_log: List[str],
    notify_fn=None,
    skill_dir: Optional[Path] = None,
    max_conflict_rounds: int = 5,
) -> Optional[str]:
    """Rebase onto target branch, resolving conflicts via Claude if needed.

    Tries origin then upstream.  When ``git rebase`` hits conflicts, Claude
    is invoked to resolve the conflicted files, they are staged, and the
    rebase is continued.  This loop repeats for up to *max_conflict_rounds*
    per remote (one round per conflicting commit).

    Returns:
        Remote name used (e.g. "origin") on success, None on total failure.
    """
    for remote in ("origin", "upstream"):
        try:
            _run_git(["git", "fetch", remote, base], cwd=project_path)
        except Exception as e:
            print(f"[rebase_pr] fetch {remote}/{base} failed: {e}", file=sys.stderr)
            continue

        # Attempt rebase
        try:
            _run_git(
                ["git", "rebase", "--autostash", f"{remote}/{base}"],
                cwd=project_path,
            )
            return remote  # Clean rebase — no conflicts
        except Exception as e:
            print(f"[rebase_pr] Rebase onto {remote}/{base} failed: {e}", file=sys.stderr)

            # Check if we're in a conflicted rebase state
            if not _has_rebase_in_progress(project_path):
                # Non-conflict failure (e.g. dirty worktree) — abort and try next
                _abort_rebase(project_path)
                continue

            # Conflict detected — try to resolve
            resolved = _resolve_rebase_conflicts(
                base, remote, project_path, context, actions_log,
                notify_fn=notify_fn, skill_dir=skill_dir,
                max_rounds=max_conflict_rounds,
            )
            if resolved:
                return remote

            # Resolution failed — abort and try next remote
            _abort_rebase(project_path)

    return None


def _has_rebase_in_progress(project_path: str) -> bool:
    """Check if a git rebase is in progress (typically due to conflicts)."""
    git_dir = Path(project_path) / ".git"
    return (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()


def _abort_rebase(project_path: str) -> None:
    """Abort a rebase in progress, ignoring errors."""
    subprocess.run(
        ["git", "rebase", "--abort"],
        stdin=subprocess.DEVNULL,
        capture_output=True, cwd=project_path,
        timeout=30,
    )


def _get_conflicted_files(project_path: str) -> List[str]:
    """Return list of files with unmerged conflicts."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            capture_output=True, text=True, cwd=project_path,
            timeout=30,
        )
        return [f.strip() for f in result.stdout.splitlines() if f.strip()]
    except Exception as e:
        print(f"[rebase_pr] failed to list conflicted files: {e}", file=sys.stderr)
        return []


def _resolve_rebase_conflicts(
    base: str,
    remote: str,
    project_path: str,
    context: dict,
    actions_log: List[str],
    notify_fn=None,
    skill_dir: Optional[Path] = None,
    max_rounds: int = 5,
) -> bool:
    """Resolve rebase conflicts via Claude, then continue the rebase.

    Each conflicting commit in the rebase may produce its own set of
    conflicts.  This function loops: resolve → stage → continue → check
    for more conflicts, up to *max_rounds* times.

    Returns True if the rebase completed successfully.
    """
    from app.cli_provider import build_full_command
    from app.config import get_model_config

    for round_num in range(1, max_rounds + 1):
        conflicted = _get_conflicted_files(project_path)
        if not conflicted:
            # No conflicts — try to continue (may already be done)
            try:
                _run_git(["git", "rebase", "--continue"], cwd=project_path)
            except Exception as e:
                print(f"[rebase_pr] rebase --continue failed: {e}", file=sys.stderr)
            # Check if rebase is still in progress
            if not _has_rebase_in_progress(project_path):
                return True
            continue

        if notify_fn:
            notify_fn(
                f"Resolving conflicts ({round_num}/{max_rounds}): "
                f"{', '.join(conflicted[:5])}"
                f"{'...' if len(conflicted) > 5 else ''}"
            )

        # Build conflict resolution prompt
        prompt = _build_conflict_resolution_prompt(
            context, conflicted, base, skill_dir=skill_dir,
        )

        # Invoke Claude to resolve conflicts
        models = get_model_config()
        cmd = build_full_command(
            prompt=prompt,
            allowed_tools=["Bash", "Read", "Write", "Glob", "Grep", "Edit"],
            model=models["mission"],
            fallback=models["fallback"],
            max_turns=15,
        )
        result = run_claude(cmd, project_path, timeout=300)

        if not result["success"]:
            print(
                f"[rebase_pr] Claude conflict resolution failed (round {round_num}): "
                f"{result['error'][:200]}",
                file=sys.stderr,
            )
            return False

        # Stage all resolved files (Claude should have done git add, but ensure it)
        remaining = _get_conflicted_files(project_path)
        if remaining:
            print(
                f"[rebase_pr] Still {len(remaining)} conflicted after Claude resolution: "
                f"{remaining}",
                file=sys.stderr,
            )
            return False

        # Continue the rebase
        try:
            # GIT_EDITOR=true prevents interactive editor for commit messages
            subprocess.run(
                ["git", "rebase", "--continue"],
                stdin=subprocess.DEVNULL,
                capture_output=True, text=True,
                cwd=project_path, timeout=60,
                env={**__import__("os").environ, "GIT_EDITOR": "true"},
            ).check_returncode()
        except subprocess.CalledProcessError:
            # May have more conflicts from subsequent commits
            if _has_rebase_in_progress(project_path):
                continue
            # Or the rebase finished despite non-zero exit
            if not _has_rebase_in_progress(project_path):
                actions_log.append(
                    f"Resolved merge conflicts ({round_num} round(s))"
                )
                return True
            return False

        # Check if rebase completed
        if not _has_rebase_in_progress(project_path):
            actions_log.append(
                f"Resolved merge conflicts ({round_num} round(s))"
            )
            return True

    print(f"[rebase_pr] Exceeded max conflict resolution rounds ({max_rounds})", file=sys.stderr)
    return False


def _build_conflict_resolution_prompt(
    context: dict,
    conflicted_files: List[str],
    base: str,
    skill_dir: Optional[Path] = None,
) -> str:
    """Build a prompt for Claude to resolve merge conflicts."""
    kwargs = dict(
        TITLE=context.get("title", ""),
        BODY=context.get("body", ""),
        BRANCH=context.get("branch", ""),
        BASE=base,
        CONFLICTED_FILES="\n".join(f"- `{f}`" for f in conflicted_files),
    )
    return load_prompt_or_skill(skill_dir, "conflict_resolution", **kwargs)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_rebase_prompt(context: dict, skill_dir: Optional[Path] = None) -> str:
    """Build a prompt for Claude to analyze and apply review feedback."""
    return _build_pr_prompt("rebase", context, skill_dir=skill_dir)


def _apply_review_feedback(
    context: dict,
    pr_number: str,
    project_path: str,
    actions_log: List[str],
    skill_dir: Optional[Path] = None,
) -> None:
    """Analyze review comments via Claude and apply requested changes."""
    from app.claude_step import run_claude_step

    prompt = _build_rebase_prompt(context, skill_dir=skill_dir)
    run_claude_step(
        prompt=prompt,
        project_path=project_path,
        commit_msg=f"rebase: apply review feedback on #{pr_number}",
        success_label="Applied review feedback",
        failure_label="Review feedback step failed",
        actions_log=actions_log,
        max_turns=20,
    )



def _checkout_pr_branch(branch: str, project_path: str) -> None:
    """Checkout the PR branch, fetching from origin or upstream.

    Uses ``git checkout -B`` to create or reset the local branch,
    ensuring a stale local branch with the same name never blocks
    the checkout.
    """
    # Try origin first, then upstream (for cross-repo PRs)
    fetch_remote = "origin"
    try:
        _run_git(["git", "fetch", "origin", branch], cwd=project_path)
    except Exception:
        try:
            _run_git(["git", "fetch", "upstream", branch], cwd=project_path)
            fetch_remote = "upstream"
        except Exception:
            raise RuntimeError(
                f"Branch `{branch}` not found on origin or upstream"
            )

    # -B creates the branch if missing, or resets it if it already exists.
    # This avoids the "branch already exists" error when a stale local
    # branch with the same name is present.
    _run_git(
        ["git", "checkout", "-B", branch, f"{fetch_remote}/{branch}"],
        cwd=project_path,
    )


def _push_with_fallback(
    branch: str,
    base: str,
    full_repo: str,
    pr_number: str,
    context: dict,
    project_path: str,
) -> dict:
    """Push rebased branch, always reusing the existing PR branch.

    Rebase never creates a new branch or PR — it always pushes to the
    same branch to recycle the existing pull request.  Tries
    ``--force-with-lease`` first, then plain ``--force`` as fallback.
    """
    actions: List[str] = []

    last_error = ""
    for remote in ("origin", "upstream"):
        # Try safe force-push first
        try:
            _run_git(
                ["git", "push", remote, branch, "--force-with-lease"],
                cwd=project_path,
            )
            actions.append(f"Force-pushed `{branch}` to {remote}")
            return {"success": True, "actions": actions, "error": ""}
        except Exception as e:
            print(f"[rebase_pr] force-with-lease to {remote} failed: {e}", file=sys.stderr)
            last_error = str(e)

        # Fall back to plain force-push (handles stale tracking refs)
        try:
            _run_git(
                ["git", "push", remote, branch, "--force"],
                cwd=project_path,
            )
            actions.append(f"Force-pushed `{branch}` to {remote}")
            return {"success": True, "actions": actions, "error": ""}
        except Exception as e:
            print(f"[rebase_pr] force push to {remote} failed: {e}", file=sys.stderr)
            last_error = str(e)

    return {
        "success": False,
        "actions": actions,
        "error": (
            f"Cannot push `{branch}`: all remotes rejected the push. "
            f"Check write permissions on the branch."
        ),
    }


def _build_rebase_comment(
    pr_number: str,
    branch: str,
    base: str,
    actions_log: List[str],
    context: dict,
    diffstat: str = "",
) -> str:
    """Build a markdown comment summarizing the rebase."""
    title = context.get("title", f"PR #{pr_number}")

    # Filter out mechanical pipeline steps for a cleaner actions list
    meaningful_actions = [
        a for a in actions_log
        if not a.startswith("Read PR comments")
        and not a.startswith("Commented on PR")
    ]
    actions_md = "\n".join(
        f"- {a}" for a in meaningful_actions
    ) if meaningful_actions else "- Rebased (no additional changes needed)"

    parts = [f"## Rebase: {title}\n"]
    parts.append(
        f"Branch `{branch}` rebased onto `{base}` and force-pushed.\n"
    )

    if diffstat:
        parts.append(f"**Diff**: {diffstat}\n")

    # Show what review feedback was addressed
    has_feedback = bool(
        context.get("review_comments", "").strip()
        or context.get("reviews", "").strip()
        or context.get("issue_comments", "").strip()
    )
    if has_feedback and any("feedback" in a.lower() for a in actions_log):
        parts.append("Review feedback was analyzed and applied.\n")

    parts.append(f"### Actions\n\n{actions_md}\n")
    parts.append("---\n_Automated by Kōan_")

    return "\n".join(parts)


def _is_conflict_failure(summary: str) -> bool:
    """Check if a rebase failure summary indicates a git conflict."""
    return "Rebase conflict" in summary or "Could not resolve conflicts" in summary


# ---------------------------------------------------------------------------
# CLI entry point — python3 -m app.rebase_pr <url> --project-path <path>
# ---------------------------------------------------------------------------

def main(argv=None):
    """CLI entry point for rebase_pr.

    On rebase conflict, automatically falls back to recreate_pr which
    creates a fresh branch from upstream and reimplements the feature.

    Returns exit code (0 = success, 1 = failure).
    """
    import argparse
    import sys

    from app.github_url_parser import parse_pr_url as _parse_url

    parser = argparse.ArgumentParser(
        description="Rebase a GitHub PR onto its target branch."
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

    skills_base = Path(__file__).resolve().parent.parent / "skills" / "core"

    success, summary = run_rebase(
        owner, repo, pr_number, cli_args.project_path,
        skill_dir=skills_base / "rebase",
    )

    if not success and _is_conflict_failure(summary):
        # Check PR state before falling back — recreate only works on open PRs
        try:
            ctx = fetch_pr_context(owner, repo, pr_number)
            pr_state = ctx.get("state", "").upper()
        except Exception as e:
            print(f"[rebase_pr] PR state check failed, proceeding with recreate: {e}", file=sys.stderr)
            pr_state = ""

        if pr_state in ("MERGED", "CLOSED"):
            print(f"{summary}\nCannot fall back to /recreate: PR #{pr_number} is {pr_state.lower()}.")
            return 1

        print(f"{summary}\nFalling back to /recreate...")
        from app.recreate_pr import run_recreate

        recreate_ok, recreate_summary = run_recreate(
            owner, repo, pr_number, cli_args.project_path,
            skill_dir=skills_base / "recreate",
        )
        print(recreate_summary)
        return 0 if recreate_ok else 1

    print(summary)
    return 0 if success else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
