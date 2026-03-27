"""
Kōan -- Pre-mission git preparation.

Ensures a project starts each mission on a fresh, up-to-date base branch.
Called before every mission execution in the agent loop.

Two public functions:
- get_upstream_remote(): Determines the canonical remote for a project.
- prepare_project_branch(): Full pre-mission git state preparation.
"""

import logging
from dataclasses import dataclass
from typing import Optional

from app.git_utils import run_git
from app.projects_config import (
    _find_project_entry,
    get_project_auto_merge,
    get_project_submit_to_repository,
    load_projects_config,
)

logger = logging.getLogger(__name__)


def detect_remote_default_branch(remote: str, project_path: str) -> str:
    """Detect the default branch for a remote.

    Resolution order:
    1. Local symbolic ref (refs/remotes/<remote>/HEAD) — fast, no network
    2. git ls-remote --symref — requires network but always accurate
    3. Falls back to "main"
    """
    # 1. Try local symbolic ref (set after clone or fetch with --set-head)
    rc, stdout, _ = run_git(
        "symbolic-ref", f"refs/remotes/{remote}/HEAD", cwd=project_path
    )
    if rc == 0 and stdout:
        # Output: refs/remotes/origin/master → extract "master"
        branch = stdout.strip().rsplit("/", 1)[-1]
        if branch:
            return branch

    # 2. Query remote (network call)
    rc, stdout, _ = run_git(
        "ls-remote", "--symref", remote, "HEAD",
        cwd=project_path, timeout=15,
    )
    if rc == 0 and stdout:
        for line in stdout.splitlines():
            if line.startswith("ref:") and "HEAD" in line:
                # Format: ref: refs/heads/master\tHEAD
                ref_part = line.split()[1]
                branch = ref_part.rsplit("/", 1)[-1]
                if branch:
                    return branch

    return "main"


@dataclass
class PrepResult:
    """Result of pre-mission git preparation."""

    remote_used: str = "origin"
    base_branch: str = "main"
    stashed: bool = False
    previous_branch: str = ""
    success: bool = True
    error: Optional[str] = None


def get_upstream_remote(
    project_path: str, project_name: str, koan_root: str
) -> str:
    """Determine the canonical remote for a project.

    Resolution order:
    1. Explicit submit_to_repository.remote from projects.yaml
    2. 'upstream' remote if it exists (common fork pattern)
    3. 'origin' fallback (default for non-fork repos)
    """
    # 1. Check explicit config
    try:
        config = load_projects_config(koan_root)
        if config:
            submit_cfg = get_project_submit_to_repository(config, project_name)
            if submit_cfg.get("remote"):
                return submit_cfg["remote"]
    except Exception as e:
        logger.warning("config load error for remote: %s", e)

    # 2. Probe for 'upstream' remote
    rc, _, _ = run_git("remote", "get-url", "upstream", cwd=project_path)
    if rc == 0:
        return "upstream"

    # 3. Fall back to 'origin'
    return "origin"


def prepare_project_branch(
    project_path: str, project_name: str, koan_root: str
) -> PrepResult:
    """Prepare a project for mission execution.

    Fetches the latest refs, stashes dirty state, checks out the base
    branch, and fast-forwards it to match the remote. Non-fatal — returns
    a PrepResult with success=False on errors rather than raising.
    """
    result = PrepResult()

    # Record current branch before any changes
    rc, current_branch, _ = run_git(
        "rev-parse", "--abbrev-ref", "HEAD", cwd=project_path
    )
    result.previous_branch = current_branch if rc == 0 else ""

    # Determine remote and base branch
    remote = get_upstream_remote(project_path, project_name, koan_root)
    result.remote_used = remote

    config_explicit = False
    try:
        config = load_projects_config(koan_root)
        if config:
            am = get_project_auto_merge(config, project_name)
            result.base_branch = am.get("base_branch", "main")
            # Check if the project explicitly configures base_branch.
            # Only project-level overrides count as explicit — the defaults
            # section provides a generic fallback that should NOT prevent
            # auto-detection for repos whose default branch differs (e.g.
            # "master" repos when defaults say "main").
            projects = config.get("projects", {}) or {}
            proj_cfg = _find_project_entry(projects, project_name) or {}
            proj_am = proj_cfg.get("git_auto_merge", {}) or {}
            if proj_am.get("base_branch"):
                config_explicit = True
    except Exception as e:
        logger.warning("config load error for base_branch: %s", e)

    base_branch = result.base_branch

    # Fetch latest refs
    rc, _, stderr = run_git(
        "fetch", remote, base_branch, cwd=project_path, timeout=30
    )
    if rc != 0 and not config_explicit:
        # Base branch was not explicitly configured — detect remote default
        detected = detect_remote_default_branch(remote, project_path)
        if detected != base_branch:
            logger.info(
                "Default branch for %s/%s is '%s', not '%s'",
                remote, project_name, detected, base_branch,
            )
            base_branch = detected
            result.base_branch = detected
            rc, _, stderr = run_git(
                "fetch", remote, base_branch, cwd=project_path, timeout=30
            )
    if rc != 0:
        result.success = False
        result.error = f"fetch failed: {stderr}"
        return result

    # Stash dirty state if needed
    rc, porcelain, _ = run_git("status", "--porcelain", cwd=project_path)
    if rc == 0 and porcelain:
        rc, _, stderr = run_git(
            "stash", "--include-untracked", cwd=project_path
        )
        if rc == 0:
            result.stashed = True
        else:
            # Abort: continuing with a dirty tree risks data loss
            # if a downstream reset --hard is needed
            result.success = False
            result.error = f"stash failed on dirty tree: {stderr}"
            return result

    # Checkout base branch
    rc, _, stderr = run_git("checkout", base_branch, cwd=project_path)
    if rc != 0:
        # Branch may not exist locally — create from remote tracking
        rc, _, stderr = run_git(
            "checkout", "-b", base_branch, f"{remote}/{base_branch}",
            cwd=project_path,
        )
        if rc != 0:
            result.success = False
            result.error = f"checkout failed: {stderr}"
            return result

    # Fast-forward to match remote
    rc, _, stderr = run_git(
        "merge", "--ff-only", f"{remote}/{base_branch}", cwd=project_path
    )
    if rc != 0:
        # Local diverged — log what will be discarded, then reset
        rc_log, diverged, _ = run_git(
            "log", f"{remote}/{base_branch}..HEAD", "--oneline",
            cwd=project_path,
        )
        if rc_log == 0 and diverged:
            logger.warning(
                "Discarding local commits on %s to match %s/%s:\n%s",
                base_branch, remote, base_branch, diverged,
            )

        rc, _, stderr = run_git(
            "reset", "--hard", f"{remote}/{base_branch}", cwd=project_path
        )
        if rc != 0:
            result.success = False
            result.error = f"reset failed: {stderr}"
            return result

    return result
