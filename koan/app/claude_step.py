"""
Kōan -- Shared helpers for the CI/CD pipeline.

Git operations, Claude Code CLI invocation, and text utilities
used by pr_review.py, rebase_pr.py, recreate_pr.py, and other
pipeline modules.
"""

import json
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

from app.cli_provider import build_full_command, run_command
from app.config import get_model_config
from app.git_utils import run_git_strict
from app.github import pr_create, run_gh
from app.prompts import load_prompt_or_skill

# Backward-compatible alias — callers should import from app.cli_provider
run_claude_command = run_command


def _run_git(cmd: list, cwd: str = None, timeout: int = 60) -> str:
    """Run a git command, raise on failure.

    Thin wrapper around git_utils.run_git_strict() preserving the
    original interface where callers pass ["git", ...] as cmd.
    """
    # Strip leading "git" if present — run_git_strict prepends it
    args = cmd[1:] if cmd and cmd[0] == "git" else cmd
    return run_git_strict(*args, cwd=cwd, timeout=timeout)


_REBASE_EXCEPTIONS = (RuntimeError, subprocess.TimeoutExpired, OSError)


def _abort_rebase_safely(project_path: str) -> None:
    """Abort a rebase in progress, ignoring errors."""
    try:
        subprocess.run(
            ["git", "rebase", "--abort"],
            stdin=subprocess.DEVNULL,
            capture_output=True, cwd=project_path,
            timeout=30,
        )
    except Exception as e:
        print(f"[claude_step] rebase --abort failed (non-fatal): {e}", file=sys.stderr)


def _ordered_remotes(preferred: Optional[str]) -> List[str]:
    """Return remote names to try, with *preferred* first if given.

    Always includes both ``origin`` and ``upstream`` (de-duplicated).
    """
    remotes: list[str] = []
    if preferred:
        remotes.append(preferred)
    for r in ("origin", "upstream"):
        if r not in remotes:
            remotes.append(r)
    return remotes


def _rebase_onto_target(
    base: str,
    project_path: str,
    preferred_remote: Optional[str] = None,
    head_remote: Optional[str] = None,
) -> Optional[str]:
    """Rebase onto target branch, trying *preferred_remote* first.

    When *preferred_remote* is given (e.g. the remote matching the PR's
    target repository), it is tried before the default ``origin`` /
    ``upstream`` fallbacks.  When *head_remote* is known and differs from
    the target remote, uses ``--onto`` to replay only the PR's commits.

    Returns:
        Remote name used (e.g. "origin" or "upstream") on success, None on failure.
    """
    for remote in _ordered_remotes(preferred_remote):
        try:
            _run_git(["git", "fetch", remote, base], cwd=project_path)
        except _REBASE_EXCEPTIONS as e:
            print(f"[claude_step] Fetch {remote}/{base} failed: {e}", file=sys.stderr)
            continue

        # When head_remote differs from target, use --onto to limit
        # replay to only the PR's commits.
        if head_remote and head_remote != remote:
            try:
                _run_git(["git", "fetch", head_remote, base], cwd=project_path)
                _run_git(
                    ["git", "rebase", "--onto", f"{remote}/{base}",
                     f"{head_remote}/{base}", "--autostash"],
                    cwd=project_path,
                )
                return remote
            except _REBASE_EXCEPTIONS as e:
                print(f"[claude_step] --onto rebase failed: {e}", file=sys.stderr)
                _abort_rebase_safely(project_path)
                # Fall through to plain rebase

        # Fallback: plain rebase
        try:
            _run_git(
                ["git", "rebase", "--autostash", f"{remote}/{base}"],
                cwd=project_path,
            )
            return remote
        except _REBASE_EXCEPTIONS as e:
            print(f"[claude_step] Rebase onto {remote}/{base} failed: {e}", file=sys.stderr)
            _abort_rebase_safely(project_path)
    return None


def strip_cli_noise(text: str) -> str:
    """Strip Claude CLI error artifacts from output.

    The CLI appends lines like 'Error: Reached max turns (N)' to stdout
    even on successful runs. These pollute journal entries and reflections
    when the output is stored verbatim.

    Returns:
        Cleaned text with CLI noise removed.
    """
    lines = text.splitlines()
    lines = [l for l in lines if not re.match(r"^Error:.*max turns", l, re.IGNORECASE)]
    return "\n".join(lines).strip()


def run_claude(cmd: list, cwd: str, timeout: int = 600) -> dict:
    """Run a Claude Code CLI command.

    Returns:
        Dict with keys: success (bool), output (str), error (str).
    """
    from app.cli_exec import run_cli_with_retry

    from app.security_audit import SUBPROCESS_EXEC, _redact_list, log_event

    try:
        result = run_cli_with_retry(
            cmd,
            capture_output=True, text=True,
            timeout=timeout, cwd=cwd,
        )
        if result.returncode != 0:
            stderr_snippet = result.stderr[-500:] if result.stderr else "no stderr"
            log_event(SUBPROCESS_EXEC, details={
                "cmd": _redact_list(cmd),
                "cwd": cwd,
                "exit_code": result.returncode,
            }, result="failure")
            return {
                "success": False,
                "output": result.stdout.strip(),
                "error": f"Exit code {result.returncode}: {stderr_snippet}",
            }
        log_event(SUBPROCESS_EXEC, details={
            "cmd": _redact_list(cmd),
            "cwd": cwd,
            "exit_code": 0,
        })
        return {
            "success": True,
            "output": result.stdout.strip(),
            "error": "",
        }
    except subprocess.TimeoutExpired:
        log_event(SUBPROCESS_EXEC, details={
            "cmd": _redact_list(cmd),
            "cwd": cwd,
        }, result="timeout")
        return {
            "success": False,
            "output": "",
            "error": f"Timeout ({timeout}s)",
        }


def commit_if_changes(project_path: str, message: str) -> bool:
    """Stage all changes and commit if there are any.

    Returns True if a commit was created.
    """
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True, cwd=project_path,
        timeout=30,
    )
    if not status.stdout.strip():
        return False

    _run_git(["git", "add", "-A"], cwd=project_path)
    _run_git(["git", "commit", "-m", message], cwd=project_path)
    return True


def run_claude_step(
    prompt: str,
    project_path: str,
    commit_msg: str,
    success_label: str,
    failure_label: str,
    actions_log: List[str],
    max_turns: int = 20,
    timeout: int = 600,
    use_skill: bool = False,
) -> bool:
    """Run a Claude Code step: invoke CLI, commit changes, log result.

    Args:
        use_skill: If True, include the Skill tool in allowed tools
                   so Claude can invoke registered skills (e.g. /refactor).

    Returns True if the step produced a commit.
    """
    models = get_model_config()

    tools = ["Bash", "Read", "Write", "Glob", "Grep", "Edit"]
    if use_skill:
        tools.append("Skill")

    cmd = build_full_command(
        prompt=prompt,
        allowed_tools=tools,
        model=models["mission"],
        fallback=models["fallback"],
        max_turns=max_turns,
    )

    result = run_claude(cmd, project_path, timeout=timeout)
    if result["success"]:
        committed = commit_if_changes(project_path, commit_msg)
        if committed and success_label:
            actions_log.append(success_label)
            return True
    elif failure_label:
        actions_log.append(f"{failure_label}: {result['error'][:200]}")
    return False


def run_project_tests(project_path: str, test_cmd: str = "make test",
                      timeout: int = 300) -> dict:
    """Run a project's test suite and return structured results.

    Args:
        project_path: Path to the project root.
        test_cmd: Shell command to run tests (default: "make test").
        timeout: Maximum seconds to wait.

    Returns:
        Dict with keys: passed (bool), output (str), details (str).
    """
    try:
        result = subprocess.run(
            shlex.split(test_cmd),
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True,
            timeout=timeout, cwd=project_path,
        )
        output = result.stdout + result.stderr
        passed = result.returncode == 0

        details = "OK" if passed else "FAILED"
        count_match = re.search(
            r'(\d+)\s+(?:tests?|passed)', output, re.IGNORECASE
        )
        if count_match:
            if passed:
                details = count_match.group(0)
            else:
                # Keep FAILED prefix with count info for context
                failed_match = re.search(r'(\d+)\s+failed', output, re.IGNORECASE)
                if failed_match:
                    details = f"{failed_match.group(0)}, {count_match.group(0)}"

        return {"passed": passed, "output": output[-3000:], "details": details}
    except subprocess.TimeoutExpired:
        return {"passed": False, "output": "", "details": f"timeout ({timeout}s)"}
    except FileNotFoundError:
        return {"passed": False, "output": "", "details": "command not found"}
    except Exception as e:
        return {"passed": False, "output": str(e), "details": str(e)[:100]}


# ---------------------------------------------------------------------------
# Shared PR pipeline helpers
# ---------------------------------------------------------------------------

def _get_current_branch(project_path: str) -> str:
    """Get the current branch name."""
    try:
        return _run_git(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=project_path,
        )
    except Exception as e:
        print(f"[claude_step] Branch detection failed, defaulting to main: {e}", file=sys.stderr)
        return "main"


def _get_diffstat(base_ref: str, project_path: str) -> str:
    """Get a compact diffstat between base_ref and HEAD.

    Returns a summary like "5 files changed, 42 insertions(+), 10 deletions(-)"
    or empty string on failure.
    """
    try:
        stat = _run_git(
            ["git", "diff", "--stat", f"{base_ref}..HEAD"],
            cwd=project_path,
            timeout=30,
        )
        # The last line of --stat output is the summary
        lines = stat.strip().splitlines()
        if lines:
            return lines[-1].strip()
    except Exception as e:
        print(f"[claude_step] diffstat failed: {e}", file=sys.stderr)
    return ""


def _safe_checkout(branch: str, project_path: str) -> None:
    """Checkout a branch without raising on failure."""
    try:
        _run_git(["git", "checkout", branch], cwd=project_path)
    except Exception as e:
        print(f"[claude_step] Safe checkout failed for {branch}: {e}", file=sys.stderr)


def wait_for_ci(
    branch: str,
    full_repo: str,
    *,
    timeout: int = 600,
    poll_interval: int = 30,
) -> Tuple[str, Optional[int], str]:
    """Poll GitHub Actions CI for a branch until completion or timeout.

    Args:
        branch: Branch name to check CI for.
        full_repo: "owner/repo" string.
        timeout: Max seconds to wait (default 10 min).
        poll_interval: Seconds between polls (default 30s).

    Returns:
        (status, run_id, logs) where:
        - status: "success", "failure", "timeout", or "none"
        - run_id: GitHub Actions run ID (None if no runs found)
        - logs: Failed job logs (empty unless status is "failure")
    """
    deadline = time.time() + timeout

    # Wait a few seconds for GitHub to register the push
    time.sleep(min(10, poll_interval))

    while time.time() < deadline:
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
            print(f"[claude_step] CI poll error: {e}", file=sys.stderr)
            time.sleep(poll_interval)
            continue

        if not runs:
            # No CI runs found for this branch — common for repos without CI
            return ("none", None, "")

        run = runs[0]
        run_id = run.get("databaseId")
        status = run.get("status", "").lower()
        conclusion = run.get("conclusion", "").lower()

        if status == "completed":
            if conclusion == "success":
                return ("success", run_id, "")

            # CI failed — fetch logs for failed jobs
            logs = _fetch_failed_logs(run_id, full_repo)
            return ("failure", run_id, logs)

        # Still running — wait and poll again
        time.sleep(poll_interval)

    return ("timeout", None, "")


def _fetch_failed_logs(run_id: int, full_repo: str, max_chars: int = 8000) -> str:
    """Fetch logs for failed jobs in a GitHub Actions run.

    Returns truncated log output for context.
    """
    try:
        raw = run_gh(
            "run", "view", str(run_id),
            "--repo", full_repo,
            "--log-failed",
        )
        if len(raw) > max_chars:
            return "... (truncated)\n" + raw[-max_chars:]
        return raw
    except Exception as e:
        return f"(Could not fetch logs: {e})"


def _is_permission_error(error_msg: str) -> bool:
    """Check if an error message indicates a permission/access problem."""
    indicators = [
        "permission", "denied", "forbidden", "403",
        "protected branch", "not allowed",
        "unable to access", "authentication failed",
    ]
    lower = error_msg.lower()
    return any(ind in lower for ind in indicators)


def _build_pr_prompt(
    prompt_name: str,
    context: dict,
    skill_dir: Optional[Path] = None,
) -> str:
    """Build a prompt for Claude to process PR feedback.

    Shared by rebase and recreate pipelines — the only difference is the
    prompt template name.

    Args:
        prompt_name: Prompt template name (e.g. "rebase", "recreate").
        context: PR context dict from fetch_pr_context().
        skill_dir: Optional skill directory for prompt resolution.
    """
    kwargs = dict(
        TITLE=context["title"],
        BODY=context.get("body", ""),
        BRANCH=context["branch"],
        BASE=context["base"],
        DIFF=context.get("diff", ""),
        REVIEW_COMMENTS=context.get("review_comments", ""),
        REVIEWS=context.get("reviews", ""),
        ISSUE_COMMENTS=context.get("issue_comments", ""),
    )
    return load_prompt_or_skill(skill_dir, prompt_name, **kwargs)


# -- Push with PR fallback (shared config) ----------------------------------

_PR_TYPE_CONFIG = {
    "rebase": {
        "force_label": "Force-pushed `{branch}`",
        "branch_suffix": "rebase-",
        "title_prefix": "[Rebase]",
        "pr_body": (
            "Supersedes #{pr_number}.\n\n"
            "This PR contains the rebased version of `{branch}` onto `{base}`.\n"
            "Original PR: {url}\n\n"
            "---\n_Automated by Kōan_"
        ),
        "crosslink": (
            "This PR has been rebased and superseded by {ref}.\n\n"
            "The new PR contains the same changes rebased onto `{base}`.\n\n"
            "---\n_Automated by Kōan_"
        ),
    },
    "recreate": {
        "force_label": "Force-pushed `{branch}` (recreated from scratch)",
        "branch_suffix": "recreate-",
        "title_prefix": "[Recreate]",
        "pr_body": (
            "Supersedes #{pr_number}.\n\n"
            "This PR contains a fresh reimplementation of the original feature, "
            "built on top of current `{base}`.\n\n"
            "The original branch had diverged too far for a clean rebase, so the "
            "feature was recreated from scratch based on the original PR's intent.\n\n"
            "Original PR: {url}\n\n"
            "---\n_Automated by Kōan_"
        ),
        "crosslink": (
            "This PR has been recreated from scratch and superseded by {ref}.\n\n"
            "The original branch had diverged too far for a clean rebase. "
            "The new PR contains a fresh reimplementation on current `{base}`.\n\n"
            "---\n_Automated by Kōan_"
        ),
    },
}


def _push_with_pr_fallback(
    branch: str,
    base: str,
    full_repo: str,
    pr_number: str,
    context: dict,
    project_path: str,
    *,
    pr_type: str = "rebase",
) -> dict:
    """Push branch, falling back to new draft PR if permission denied.

    Shared by rebase and recreate pipelines.

    Args:
        pr_type: "rebase" or "recreate" — controls labels, prefix, and body text.

    Returns:
        dict with keys: success, actions, error, new_pr_url (optional).
    """
    actions: List[str] = []
    cfg = _PR_TYPE_CONFIG.get(pr_type, _PR_TYPE_CONFIG["rebase"])

    # Option 1: Try force-pushing to the existing branch
    try:
        _run_git(
            ["git", "push", "origin", branch, "--force-with-lease"],
            cwd=project_path,
        )
        actions.append(cfg["force_label"].format(branch=branch))
        return {"success": True, "actions": actions, "error": ""}
    except Exception as push_error:
        error_msg = str(push_error)

    # Option 2: Permission denied — create a new draft PR
    if not _is_permission_error(error_msg):
        return {"success": False, "actions": actions, "error": error_msg}

    from app.config import get_branch_prefix
    prefix = get_branch_prefix()
    new_branch = f"{prefix}{cfg['branch_suffix']}{branch.replace('/', '-')}"
    try:
        _run_git(["git", "checkout", "-b", new_branch], cwd=project_path)
        _run_git(["git", "push", "-u", "origin", new_branch], cwd=project_path)
        actions.append(
            f"Created new branch `{new_branch}` (no push permission on `{branch}`)"
        )

        title = context.get("title", f"{cfg['title_prefix'].strip('[]')} of #{pr_number}")
        pr_body = cfg["pr_body"].format(
            pr_number=pr_number, branch=branch, base=base,
            url=context.get("url", f"#{pr_number}"),
        )
        new_pr_url = pr_create(
            title=f"{cfg['title_prefix']} {title}",
            body=pr_body,
            draft=True,
            base=base,
            repo=full_repo,
            head=new_branch,
        )
        actions.append(f"Created draft PR: {new_pr_url.strip()}")

        # Cross-link on original PR
        new_pr_match = re.search(r'/pull/(\d+)', new_pr_url)
        new_pr_ref = new_pr_match.group(0) if new_pr_match else new_pr_url.strip()

        try:
            run_gh(
                "pr", "comment", pr_number,
                "--repo", full_repo,
                "--body", cfg["crosslink"].format(ref=new_pr_ref, base=base),
            )
            actions.append("Cross-linked original PR")
        except Exception as e:
            print(f"[{pr_type}_pr] Cross-link comment failed: {e}", file=sys.stderr)

        return {
            "success": True,
            "actions": actions,
            "error": "",
            "new_pr_url": new_pr_url.strip(),
        }

    except Exception as e:
        return {
            "success": False,
            "actions": actions,
            "error": f"Failed to create fallback PR: {e}",
        }
