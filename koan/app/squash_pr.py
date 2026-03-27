"""
Koan -- Pull Request squash workflow.

Squashes all commits on a PR branch into a single clean commit,
generates a descriptive commit message via Claude, force-pushes,
and updates the PR title/description on GitHub.

Pipeline:
1. Fetch PR metadata from GitHub
2. Checkout the PR branch locally
3. Squash all commits since the merge-base into one
4. Generate commit message, PR title, and description via Claude
5. Force-push the squashed branch
6. Update PR title and description on GitHub
7. Comment on the PR with a summary
"""

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from app.claude_step import (
    _get_current_branch,
    _run_git,
    _safe_checkout,
    run_claude,
    strip_cli_noise,
)
from app.cli_provider import build_full_command
from app.config import get_model_config
from app.git_utils import ordered_remotes as _ordered_remotes
from app.github import run_gh
from app.prompts import load_prompt_or_skill
from app.rebase_pr import _find_remote_for_repo, fetch_pr_context
from app.utils import truncate_text


def _count_commits_since_base(
    base_ref: str, project_path: str,
) -> int:
    """Count commits between merge-base and HEAD."""
    try:
        merge_base = _run_git(
            ["git", "merge-base", base_ref, "HEAD"],
            cwd=project_path,
        ).strip()
        log = _run_git(
            ["git", "rev-list", f"{merge_base}..HEAD"],
            cwd=project_path,
        ).strip()
        return len(log.splitlines()) if log else 0
    except Exception as e:
        print(f"[squash_pr] merge-base count failed: {e}", file=sys.stderr)
        return 0


def _squash_commits(
    base_ref: str, project_path: str, message: str,
) -> bool:
    """Squash all commits since merge-base into a single commit.

    Uses git reset --soft to the merge-base, then commits all staged
    changes as one commit.

    Returns True if squash produced a commit.
    """
    merge_base = _run_git(
        ["git", "merge-base", base_ref, "HEAD"],
        cwd=project_path,
    ).strip()

    _run_git(["git", "reset", "--soft", merge_base], cwd=project_path)
    _run_git(["git", "commit", "-m", message], cwd=project_path)
    return True


def _generate_squash_text(
    context: dict, diff: str, skill_dir: Optional[Path] = None,
) -> dict:
    """Use Claude to generate commit message, PR title, and description.

    Returns dict with keys: commit_message, pr_title, pr_description.
    Falls back to PR metadata if Claude fails.
    """
    kwargs = dict(
        TITLE=context.get("title", ""),
        BODY=context.get("body", ""),
        BRANCH=context.get("branch", ""),
        BASE=context.get("base", "main"),
        DIFF=truncate_text(diff, 12000),
    )
    prompt = load_prompt_or_skill(skill_dir, "squash", **kwargs)

    models = get_model_config()
    cmd = build_full_command(
        prompt=prompt,
        allowed_tools=[],
        model=models.get("lightweight", models["mission"]),
        fallback=models["fallback"],
        max_turns=1,
    )

    result = run_claude(cmd, ".", timeout=120)

    if result["success"]:
        return _parse_squash_output(result["output"], context)

    # Fallback: use existing PR metadata
    return {
        "commit_message": context.get("title", "squash commits"),
        "pr_title": context.get("title", ""),
        "pr_description": context.get("body", ""),
    }


def _parse_squash_output(output: str, context: dict) -> dict:
    """Parse Claude's structured output into components."""
    output = strip_cli_noise(output)

    commit_msg = _extract_between(output, "===COMMIT_MESSAGE===", "===PR_TITLE===")
    pr_title = _extract_between(output, "===PR_TITLE===", "===PR_DESCRIPTION===")
    pr_desc = _extract_between(output, "===PR_DESCRIPTION===", "===END===")

    return {
        "commit_message": commit_msg or context.get("title", "squash commits"),
        "pr_title": pr_title or context.get("title", ""),
        "pr_description": pr_desc or context.get("body", ""),
    }


def _extract_between(text: str, start_marker: str, end_marker: str) -> str:
    """Extract text between two markers."""
    start_idx = text.find(start_marker)
    if start_idx == -1:
        return ""
    start_idx += len(start_marker)
    end_idx = text.find(end_marker, start_idx)
    if end_idx == -1:
        return text[start_idx:].strip()
    return text[start_idx:end_idx].strip()


def _force_push(branch: str, project_path: str) -> str:
    """Force-push branch, trying remotes in order.

    Returns remote name used on success. Raises on total failure.
    """
    for remote in _ordered_remotes(None):
        try:
            _run_git(
                ["git", "push", remote, branch, "--force-with-lease"],
                cwd=project_path,
            )
            return remote
        except Exception as e:
            print(f"[squash_pr] force-with-lease failed on {remote}: {e}", file=sys.stderr)
            try:
                _run_git(
                    ["git", "push", remote, branch, "--force"],
                    cwd=project_path,
                )
                return remote
            except Exception as e2:
                print(f"[squash_pr] force push failed on {remote}: {e2}", file=sys.stderr)
                continue
    raise RuntimeError(f"Cannot push `{branch}`: all remotes rejected the push.")


def run_squash(
    owner: str,
    repo: str,
    pr_number: str,
    project_path: str,
    notify_fn=None,
    skill_dir: Optional[Path] = None,
) -> Tuple[bool, str]:
    """Execute the squash pipeline for a pull request.

    Steps:
        1. Fetch PR context from GitHub
        2. Checkout the PR branch locally
        3. Squash all commits into one
        4. Generate commit message + PR metadata via Claude
        5. Force-push the squashed branch
        6. Update PR title and description
        7. Comment on the PR

    Returns:
        (success, summary) tuple.
    """
    if notify_fn is None:
        from app.notify import send_telegram
        notify_fn = send_telegram

    full_repo = f"{owner}/{repo}"
    actions_log: List[str] = []

    # -- Step 1: Fetch PR context --
    notify_fn(f"Reading PR #{pr_number}...")
    try:
        context = fetch_pr_context(owner, repo, pr_number)
    except Exception as e:
        return False, f"Failed to fetch PR context: {e}"

    pr_state = context.get("state", "").upper()
    if pr_state in ("MERGED", "CLOSED"):
        msg = f"PR #{pr_number} is already {pr_state.lower()} — skipping squash."
        notify_fn(msg)
        return True, msg

    if not context["branch"]:
        return False, "Could not determine PR branch name."

    branch = context["branch"]
    base = context["base"]

    # Determine remote for the PR's target repo
    base_remote = _find_remote_for_repo(owner, repo, project_path)

    # Determine remote for the PR's head branch (fork)
    head_owner = context.get("head_owner", "")
    head_remote = (
        _find_remote_for_repo(head_owner, repo, project_path)
        if head_owner else None
    )

    # -- Step 2: Checkout PR branch --
    notify_fn(f"Checking out `{branch}`...")
    original_branch = _get_current_branch(project_path)

    try:
        fetch_remote = _checkout_pr_branch(
            branch, project_path,
            head_remote=head_remote,
            head_owner=head_owner,
            repo=repo,
        )
    except Exception as e:
        return False, f"Failed to checkout branch `{branch}`: {e}"

    # Fetch the base branch to get an accurate merge-base
    effective_remote = base_remote or fetch_remote or "origin"
    try:
        _run_git(["git", "fetch", effective_remote, base], cwd=project_path)
    except Exception as e_fetch:
        print(f"[squash_pr] fetch base from {effective_remote} failed: {e_fetch}", file=sys.stderr)
        # Try origin as fallback
        try:
            _run_git(["git", "fetch", "origin", base], cwd=project_path)
            effective_remote = "origin"
        except Exception as e:
            _safe_checkout(original_branch, project_path)
            return False, f"Failed to fetch base branch `{base}`: {e}"

    base_ref = f"{effective_remote}/{base}"

    # -- Step 3: Count commits and check if squash is needed --
    commit_count = _count_commits_since_base(base_ref, project_path)
    if commit_count <= 1:
        msg = (
            f"PR #{pr_number} already has {commit_count} commit(s) — "
            f"nothing to squash."
        )
        _safe_checkout(original_branch, project_path)
        notify_fn(msg)
        return True, msg

    actions_log.append(f"Squashed {commit_count} commits into 1")

    # -- Step 4: Get the diff for text generation --
    notify_fn(f"Squashing {commit_count} commits on `{branch}`...")
    try:
        diff = _run_git(
            ["git", "diff", f"{base_ref}..HEAD"],
            cwd=project_path, timeout=30,
        )
    except Exception as e:
        print(f"[squash_pr] diff generation failed: {e}", file=sys.stderr)
        diff = ""

    # -- Step 5: Generate commit message + PR metadata --
    notify_fn("Generating commit message and PR description...")
    squash_text = _generate_squash_text(
        context, diff, skill_dir=skill_dir,
    )

    # -- Step 6: Squash --
    try:
        _squash_commits(
            base_ref, project_path,
            squash_text["commit_message"],
        )
    except Exception as e:
        _safe_checkout(original_branch, project_path)
        return False, f"Squash failed: {e}"

    # -- Step 7: Force-push --
    notify_fn(f"Force-pushing `{branch}`...")
    try:
        push_remote = _force_push(branch, project_path)
        actions_log.append(f"Force-pushed `{branch}` to {push_remote}")
    except Exception as e:
        _safe_checkout(original_branch, project_path)
        return False, f"Push failed: {e}"

    # -- Step 8: Update PR title and description --
    new_title = squash_text["pr_title"]
    new_desc = squash_text["pr_description"]

    if new_title:
        try:
            run_gh(
                "pr", "edit", pr_number,
                "--repo", full_repo,
                "--title", new_title,
            )
            actions_log.append(f"Updated PR title")
        except Exception as e:
            actions_log.append(f"Title update failed (non-fatal): {str(e)[:100]}")

    if new_desc:
        try:
            run_gh(
                "pr", "edit", pr_number,
                "--repo", full_repo,
                "--body", new_desc,
            )
            actions_log.append(f"Updated PR description")
        except Exception as e:
            actions_log.append(
                f"Description update failed (non-fatal): {str(e)[:100]}"
            )

    # -- Step 9: Comment on the PR --
    comment_body = _build_squash_comment(
        pr_number, branch, base, commit_count, actions_log,
        squash_text,
    )
    try:
        run_gh(
            "pr", "comment", pr_number,
            "--repo", full_repo,
            "--body", comment_body,
        )
        actions_log.append("Commented on PR")
    except Exception as e:
        actions_log.append(f"Comment failed (non-fatal): {str(e)[:100]}")

    # Restore original branch
    _safe_checkout(original_branch, project_path)

    summary = f"PR #{pr_number} squashed.\n" + "\n".join(
        f"- {a}" for a in actions_log
    )
    return True, summary


def _checkout_pr_branch(
    branch: str,
    project_path: str,
    head_remote: Optional[str] = None,
    head_owner: str = "",
    repo: str = "",
) -> str:
    """Checkout the PR branch, fetching from the appropriate remote.

    Returns the remote name used for the fetch.
    """
    remotes = _ordered_remotes(head_remote)

    for remote in remotes:
        try:
            _run_git(["git", "fetch", remote, branch], cwd=project_path)
            _run_git(
                ["git", "checkout", "-B", branch, f"{remote}/{branch}"],
                cwd=project_path,
            )
            return remote
        except Exception as e:
            print(f"[squash_pr] checkout from {remote} failed: {e}", file=sys.stderr)
            continue

    # Try adding fork remote if known
    if head_owner and repo:
        fork_remote = f"fork-{head_owner}"
        fork_url = f"https://github.com/{head_owner}/{repo}.git"
        try:
            _run_git(
                ["git", "remote", "add", fork_remote, fork_url],
                cwd=project_path,
            )
        except Exception as e:
            print(f"[squash_pr] add fork remote failed: {e}", file=sys.stderr)
        try:
            _run_git(["git", "fetch", fork_remote, branch], cwd=project_path)
            _run_git(
                ["git", "checkout", "-B", branch, f"{fork_remote}/{branch}"],
                cwd=project_path,
            )
            return fork_remote
        except Exception as e:
            print(f"[squash_pr] fetch from fork remote failed: {e}", file=sys.stderr)

    raise RuntimeError(
        f"Branch `{branch}` not found on any remote "
        f"(tried {', '.join(remotes)})"
    )


def _build_squash_comment(
    pr_number: str,
    branch: str,
    base: str,
    commit_count: int,
    actions_log: List[str],
    squash_text: dict,
) -> str:
    """Build a markdown comment summarizing the squash."""
    meaningful_actions = [
        a for a in actions_log
        if not a.startswith("Commented on PR")
    ]
    actions_md = "\n".join(f"- {a}" for a in meaningful_actions)

    parts = [
        f"## Squash: {commit_count} commits → 1\n",
        f"Branch `{branch}` was squashed and force-pushed.\n",
        f"### Commit message\n\n```\n{squash_text['commit_message']}\n```\n",
    ]

    if actions_md:
        parts.append(f"### Actions\n\n{actions_md}\n")

    parts.append("---\n_Automated by Koan_")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# CLI entry point -- python3 -m app.squash_pr <url> --project-path <path>
# ---------------------------------------------------------------------------

def main(argv=None):
    """CLI entry point for squash_pr.

    Returns exit code (0 = success, 1 = failure).
    """
    import argparse

    from app.github_url_parser import parse_pr_url as _parse_url

    parser = argparse.ArgumentParser(
        description="Squash all commits on a GitHub PR into one."
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

    success, summary = run_squash(
        owner, repo, pr_number, cli_args.project_path,
        skill_dir=skills_base / "squash",
    )

    print(summary)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
