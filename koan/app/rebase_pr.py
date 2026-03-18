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
import time
from pathlib import Path
from typing import List, Optional, Tuple

from app.claude_step import (
    _build_pr_prompt,
    _fetch_failed_logs,
    _get_current_branch,
    _get_diffstat,
    _run_git,
    _safe_checkout,
    commit_if_changes,
    run_claude,
    run_claude_step,
    wait_for_ci,
)
from app.github import run_gh
from app.prompts import load_prompt, load_prompt_or_skill, load_skill_prompt  # noqa: F401 — safety import
from app.utils import _GITHUB_REMOTE_RE, truncate_text


def fetch_pr_context(owner: str, repo: str, pr_number: str) -> dict:
    """Fetch PR details, diff, and all comments via gh CLI.

    Returns a dict with keys: title, body, branch, base, state, author, url,
    diff, review_comments, reviews, issue_comments.
    """
    full_repo = f"{owner}/{repo}"

    # Fetch PR metadata
    pr_json = run_gh(
        "pr", "view", pr_number, "--repo", full_repo, "--json",
        "title,body,headRefName,baseRefName,state,author,url,headRepositoryOwner",
    )

    # Fetch review comment count from REST API for pending review detection.
    # GitHub counts pending (unsubmitted) review comments in PR metadata but
    # the comments endpoints don't return them to other users.
    # Retry once on transient failures — falling back to 0 incorrectly hides
    # pending reviews, causing the bot to miss unsubmitted review feedback.
    api_review_comment_count = 0
    for _attempt in range(2):
        try:
            count_json = run_gh(
                "api", f"repos/{full_repo}/pulls/{pr_number}",
                "--jq", ".review_comments",
            )
            api_review_comment_count = int(count_json.strip()) if count_json.strip() else 0
            break
        except (RuntimeError, ValueError):
            if _attempt == 0:
                time.sleep(2)
                continue
            api_review_comment_count = 0

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

    # Detect pending (unsubmitted) reviews: GitHub counts pending review
    # comments in the PR metadata but the API doesn't return them to other
    # users.  When the count is positive but fetched comments are empty,
    # there are invisible pending reviews.
    fetched_comment_count = len(comments_json.strip().splitlines()) if comments_json.strip() else 0
    has_pending_reviews = api_review_comment_count > 0 and fetched_comment_count == 0

    return {
        "title": metadata.get("title", ""),
        "body": metadata.get("body", ""),
        "branch": metadata.get("headRefName", ""),
        "base": metadata.get("baseRefName", "main"),
        "state": metadata.get("state", ""),
        "author": metadata.get("author", {}).get("login", ""),
        "head_owner": metadata.get("headRepositoryOwner", {}).get("login", ""),
        "url": metadata.get("url", ""),
        "diff": truncate_text(diff, 8000),
        "review_comments": truncate_text(comments_json, 4000),
        "reviews": truncate_text(reviews_json, 3000),
        "issue_comments": truncate_text(issue_comments, 3000),
        "has_pending_reviews": has_pending_reviews,
    }


def _find_remote_for_repo(
    owner: str, repo: str, project_path: str,
) -> Optional[str]:
    """Find the local git remote name that matches a GitHub owner/repo.

    Compares each remote's URL against the target ``owner/repo`` (case-insensitive).
    Returns the remote name (e.g. ``"upstream"``) or ``None`` if no match.
    """
    target = f"{owner}/{repo}".lower()
    try:
        result = subprocess.run(
            ["git", "remote", "-v"],
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True, cwd=project_path, timeout=5,
        )
        if result.returncode != 0:
            return None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None

    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        remote_name, url = parts[0], parts[1]
        match = _GITHUB_REMOTE_RE.search(url)
        if match:
            slug = f"{match.group(1)}/{match.group(2)}".lower()
            if slug == target:
                return remote_name
    return None


def _ordered_remotes(preferred: Optional[str]) -> List[str]:
    """Return remote names to try, with *preferred* first if given.

    Always includes both ``origin`` and ``upstream`` (de-duplicated).
    """
    remotes = []
    if preferred:
        remotes.append(preferred)
    for r in ("origin", "upstream"):
        if r not in remotes:
            remotes.append(r)
    return remotes


def _has_review_feedback(context: dict) -> bool:
    """Check if the PR context contains any review feedback."""
    return bool(
        context.get("review_comments", "").strip()
        or context.get("reviews", "").strip()
        or context.get("issue_comments", "").strip()
    )


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

    # Skip if the PR is already merged or closed — nothing to rebase
    pr_state = context.get("state", "").upper()
    if pr_state in ("MERGED", "CLOSED"):
        msg = f"PR #{pr_number} is already {pr_state.lower()} — skipping rebase."
        notify_fn(msg)
        return True, msg

    if not context["branch"]:
        return False, "Could not determine PR branch name."

    # Warn about pending (unsubmitted) reviews we cannot read
    if context.get("has_pending_reviews"):
        notify_fn(
            f"⚠️ PR #{pr_number} has pending (unsubmitted) review comments "
            f"that are invisible to the API. The rebase will proceed but may "
            f"miss some feedback. Consider submitting the pending review on "
            f"GitHub."
        )
        actions_log.append("Warning: pending (unsubmitted) review comments detected")

    branch = context["branch"]
    base = context["base"]

    # Determine which local remote corresponds to the PR's target repo
    # so we rebase against the correct upstream, not a stale fork.
    base_remote = _find_remote_for_repo(owner, repo, project_path)

    # Determine which remote hosts the PR's head branch (the fork)
    head_owner = context.get("head_owner", "")
    head_remote = _find_remote_for_repo(head_owner, repo, project_path) if head_owner else None

    # Log comment summary for awareness
    comment_summary = build_comment_summary(context)
    if comment_summary and "No comments" not in comment_summary:
        actions_log.append("Read PR comments and review feedback")

    # ── Step 2: Checkout the PR branch ────────────────────────────────
    notify_fn(f"Checking out `{branch}`...")

    # Save current branch to restore later
    original_branch = _get_current_branch(project_path)

    try:
        fetch_remote = _checkout_pr_branch(branch, project_path)
    except Exception as e:
        return False, f"Failed to checkout branch `{branch}`: {e}"

    # Use API-discovered head_remote, fall back to checkout's fetch_remote
    effective_head_remote = head_remote or fetch_remote

    # ── Step 3: Rebase onto target branch ─────────────────────────────
    notify_fn(f"Rebasing `{branch}` onto `{base}`...")
    rebase_remote = _rebase_with_conflict_resolution(
        base, project_path, context, actions_log,
        notify_fn=notify_fn, skill_dir=skill_dir,
        preferred_remote=base_remote,
        head_remote=effective_head_remote,
    )
    if rebase_remote:
        actions_log.append(f"Rebased `{branch}` onto `{rebase_remote}/{base}`")
    else:
        _safe_checkout(original_branch, project_path)
        return False, f"Rebase failed on `{base}` (tried origin and upstream). Could not resolve conflicts."

    # ── Step 4: Analyze review comments and apply changes ──────────────
    if _has_review_feedback(context):
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
        branch, base, full_repo, pr_number, context, project_path,
        head_remote=effective_head_remote,
    )
    actions_log.extend(push_result["actions"])

    if not push_result["success"]:
        _safe_checkout(original_branch, project_path)
        return False, (
            f"Push failed: {push_result.get('error', 'unknown')}\n\n"
            f"Actions completed:\n" +
            "\n".join(f"- {a}" for a in actions_log)
        )

    # ── Step 7: Check CI and fix failures ──────────────────────────────
    ci_section = _run_ci_check_and_fix(
        branch=branch,
        base=base,
        full_repo=full_repo,
        pr_number=pr_number,
        project_path=project_path,
        context=context,
        actions_log=actions_log,
        notify_fn=notify_fn,
        skill_dir=skill_dir,
    )

    # ── Step 8: Comment on the PR ─────────────────────────────────────
    comment_body = _build_rebase_comment(
        pr_number, branch, base, actions_log, context,
        diffstat=diffstat,
        ci_section=ci_section,
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
    preferred_remote: Optional[str] = None,
    head_remote: Optional[str] = None,
) -> Optional[str]:
    """Rebase onto target branch, resolving conflicts via Claude if needed.

    Tries the *preferred_remote* first (matched from the PR's target repo),
    then falls back to ``origin`` and ``upstream``.  When *head_remote* is
    known and differs from the target remote, uses ``--onto`` to replay only
    the PR's commits (between ``head_remote/base`` and HEAD) onto the target.

    When ``git rebase`` hits conflicts, Claude is invoked to resolve the
    conflicted files, they are staged, and the rebase is continued.  This
    loop repeats for up to *max_conflict_rounds* per remote (one round per
    conflicting commit).

    Returns:
        Remote name used (e.g. "origin") on success, None on total failure.
    """
    for remote in _ordered_remotes(preferred_remote):
        try:
            _run_git(["git", "fetch", remote, base], cwd=project_path)
        except Exception as e:
            print(f"[rebase_pr] fetch {remote}/{base} failed: {e}", file=sys.stderr)
            continue

        # When head_remote differs from the target remote, use --onto to
        # limit replay to only the PR's commits (avoids replaying upstream
        # history when the fork has diverged).
        if head_remote and head_remote != remote:
            try:
                _run_git(["git", "fetch", head_remote, base], cwd=project_path)
                _run_git(
                    ["git", "rebase", "--onto", f"{remote}/{base}",
                     f"{head_remote}/{base}", "--autostash"],
                    cwd=project_path,
                )
                return remote  # Clean --onto rebase
            except Exception as e:
                print(f"[rebase_pr] --onto rebase failed: {e}", file=sys.stderr)
                # Check if we're in a conflicted rebase state from --onto
                if _has_rebase_in_progress(project_path):
                    resolved = _resolve_rebase_conflicts(
                        base, remote, project_path, context, actions_log,
                        notify_fn=notify_fn, skill_dir=skill_dir,
                        max_rounds=max_conflict_rounds,
                    )
                    if resolved:
                        return remote
                    _abort_rebase(project_path)
                # Fall through to plain rebase

        # Fallback: plain rebase (same repo PR, or --onto failed)
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


_UNMERGED_STATUSES = frozenset({"DD", "AU", "UD", "UA", "DU", "AA", "UU"})


def _get_conflicted_files(project_path: str) -> List[str]:
    """Return list of files with unmerged conflicts.

    Uses ``git status --porcelain`` which explicitly reports the merge state
    of each index entry.  Previous implementation used
    ``git diff --name-only --diff-filter=U`` which can silently return
    incomplete results during complex rebase operations (e.g. ``--onto``
    rebases or branches with merge commits being linearised).
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True, cwd=project_path,
            timeout=30,
        )
        files = []
        for line in result.stdout.splitlines():
            if len(line) >= 4 and line[:2] in _UNMERGED_STATUSES:
                files.append(line[3:].strip())
        return files
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

MAX_CI_FIX_ATTEMPTS = 2


def _check_pr_state(pr_number: str, full_repo: str) -> tuple:
    """Query current PR state and mergeable status.

    Returns:
        (state, mergeable) tuple where state is e.g. "OPEN", "MERGED", "CLOSED"
        and mergeable is e.g. "MERGEABLE", "CONFLICTING", "UNKNOWN".
    """
    try:
        raw = run_gh(
            "pr", "view", pr_number, "--repo", full_repo,
            "--json", "state,mergeable",
        )
        data = json.loads(raw) if raw.strip() else {}
        return (
            data.get("state", "UNKNOWN"),
            data.get("mergeable", "UNKNOWN"),
        )
    except Exception as e:
        print(f"[rebase] PR state check failed: {e}", file=sys.stderr)
        return ("UNKNOWN", "UNKNOWN")


def _force_push(remote: str, branch: str, project_path: str) -> None:
    """Force-push branch, trying --force-with-lease first then --force.

    Raises on total failure.
    """
    try:
        _run_git(
            ["git", "push", remote, branch, "--force-with-lease"],
            cwd=project_path,
        )
    except Exception as e:
        print(f"[rebase_pr] --force-with-lease failed, falling back to --force: {e}", file=sys.stderr)
        _run_git(
            ["git", "push", remote, branch, "--force"],
            cwd=project_path,
        )


def _run_ci_check_and_fix(
    branch: str,
    base: str,
    full_repo: str,
    pr_number: str,
    project_path: str,
    context: dict,
    actions_log: List[str],
    notify_fn,
    skill_dir: Optional[Path] = None,
) -> str:
    """Poll CI after push, attempt fixes if failing. Returns CI section for PR comment."""

    pr_url = context.get("url") or f"https://github.com/{full_repo}/pull/{pr_number}"

    notify_fn(f"Checking CI on [{branch}]({pr_url})...")
    ci_status, run_id, ci_logs = wait_for_ci(branch, full_repo)

    if ci_status == "none":
        actions_log.append("No CI runs found")
        return ""

    if ci_status == "success":
        actions_log.append("CI passed")
        return "CI passed."

    if ci_status == "timeout":
        actions_log.append("CI polling timed out")
        return "CI still running (timed out waiting)."

    # CI failed — attempt fixes
    for attempt in range(1, MAX_CI_FIX_ATTEMPTS + 1):
        # Check if PR has been merged or has conflicts before attempting fix
        pr_state, mergeable = _check_pr_state(pr_number, full_repo)

        if pr_state == "MERGED":
            actions_log.append("PR already merged — skipping CI fix")
            return "PR already merged — CI fix skipped."

        if mergeable == "CONFLICTING":
            actions_log.append("PR has merge conflicts — skipping CI fix")
            return "PR has merge conflicts — CI fix skipped (rebase needed)."

        notify_fn(f"CI failed on [{pr_url}]({pr_url}). Fix attempt {attempt}/{MAX_CI_FIX_ATTEMPTS}...")
        actions_log.append(f"CI failed (attempt {attempt})")

        # Build CI fix prompt
        rebase_remote = "origin"
        diff = ""
        try:
            diff = _run_git(
                ["git", "diff", f"{rebase_remote}/{base}..HEAD"],
                cwd=project_path, timeout=30,
            )
        except Exception as e:
            print(f"[rebase] diff fetch failed: {e}", file=sys.stderr)
        diff = truncate_text(diff, 8000)

        ci_fix_prompt = _build_ci_fix_prompt(
            context, ci_logs, diff, skill_dir=skill_dir,
        )

        # Run Claude to fix the CI failures
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
            # Claude didn't produce changes — nothing to push
            break

        # Force-push the fix
        try:
            _force_push("origin", branch, project_path)
        except Exception as e:
            actions_log.append(f"Push after CI fix failed: {str(e)[:100]}")
            break

        actions_log.append(f"Pushed CI fix (attempt {attempt})")

        # Re-check CI
        notify_fn(f"Re-checking CI on [{pr_url}]({pr_url}) after fix attempt {attempt}...")
        ci_status, run_id, ci_logs = wait_for_ci(branch, full_repo)

        if ci_status == "success":
            actions_log.append(f"CI passed after fix attempt {attempt}")
            return f"CI failed initially, fixed on attempt {attempt}."

        if ci_status in ("none", "timeout"):
            actions_log.append(f"CI {ci_status} after fix attempt {attempt}")
            return f"CI fix pushed (attempt {attempt}), CI status: {ci_status}."

    # Exhausted retries — report failure with log excerpt
    log_excerpt = ci_logs[:2000] if ci_logs else "(no logs available)"
    actions_log.append(f"CI still failing after {MAX_CI_FIX_ATTEMPTS} fix attempts")
    return (
        f"CI still failing after {MAX_CI_FIX_ATTEMPTS} fix attempts.\n\n"
        f"<details><summary>Last failure logs</summary>\n\n"
        f"```\n{log_excerpt}\n```\n\n</details>"
    )


def _build_ci_fix_prompt(
    context: dict,
    ci_logs: str,
    diff: str,
    skill_dir: Optional[Path] = None,
) -> str:
    """Build a prompt for Claude to fix CI failures."""
    kwargs = dict(
        TITLE=context.get("title", ""),
        BRANCH=context.get("branch", ""),
        BASE=context.get("base", ""),
        CI_LOGS=truncate_text(ci_logs, 6000),
        DIFF=truncate_text(diff, 8000),
    )
    return load_prompt_or_skill(skill_dir, "ci_fix", **kwargs)


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



def _checkout_pr_branch(branch: str, project_path: str) -> str:
    """Checkout the PR branch, fetching from origin or upstream.

    Uses ``git checkout -B`` to create or reset the local branch,
    ensuring a stale local branch with the same name never blocks
    the checkout.

    Returns:
        The remote name used for the fetch (e.g. ``"origin"`` or ``"upstream"``).
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
    return fetch_remote


def _push_with_fallback(
    branch: str,
    base: str,
    full_repo: str,
    pr_number: str,
    context: dict,
    project_path: str,
    head_remote: Optional[str] = None,
) -> dict:
    """Push rebased branch, always reusing the existing PR branch.

    Rebase never creates a new branch or PR — it always pushes to the
    same branch to recycle the existing pull request.  Tries *head_remote*
    first (where the PR branch lives), then ``origin`` and ``upstream``.
    Uses ``--force-with-lease`` first, then plain ``--force`` as fallback.
    """
    actions: List[str] = []
    remotes = _ordered_remotes(head_remote)
    last_error = ""
    for remote in remotes:
        try:
            _force_push(remote, branch, project_path)
            actions.append(f"Force-pushed `{branch}` to {remote}")
            return {"success": True, "actions": actions, "error": ""}
        except Exception as e:
            print(f"[rebase_pr] push to {remote} failed: {e}", file=sys.stderr)
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
    ci_section: str = "",
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
    if _has_review_feedback(context) and any("feedback" in a.lower() for a in actions_log):
        parts.append("Review feedback was analyzed and applied.\n")

    parts.append(f"### Actions\n\n{actions_md}\n")

    if ci_section:
        parts.append(f"### CI\n\n{ci_section}\n")

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
