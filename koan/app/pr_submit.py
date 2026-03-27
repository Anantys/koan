"""Shared helpers for draft PR submission after skill execution.

Used by fix_runner.py and implement_runner.py to avoid duplicating
the post-execution PR submission pipeline (branch check, push,
fork detection, PR creation, issue comment).
"""

import logging
import os
import subprocess
from pathlib import Path
from typing import List, Optional

from app.git_utils import (
    get_commit_subjects as _git_get_commit_subjects,
    get_current_branch as _git_get_current_branch,
    run_git_strict,
)
from app.github import detect_parent_repo, run_gh, pr_create
from app.projects_config import resolve_base_branch

logger = logging.getLogger(__name__)


def guess_project_name(project_path: str) -> str:
    """Extract project name from the directory path."""
    return Path(project_path).name


def get_current_branch(project_path: str) -> str:
    """Return the current git branch name, or 'main' on error.

    Delegates to :func:`app.git_utils.get_current_branch`.
    """
    return _git_get_current_branch(cwd=project_path)


def get_commit_subjects(project_path: str, base_branch: str = "main") -> List[str]:
    """Return commit subject lines from base_branch..HEAD.

    Delegates to :func:`app.git_utils.get_commit_subjects`.
    """
    return _git_get_commit_subjects(cwd=project_path, base_branch=base_branch)


def get_fork_owner(project_path: str) -> str:
    """Return the GitHub owner login of the current repo."""
    try:
        return run_gh(
            "repo", "view", "--json", "owner", "--jq", ".owner.login",
            cwd=project_path, timeout=15,
        ).strip()
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        logger.debug("Failed to get fork owner: %s", e)
        return ""


def resolve_submit_target(
    project_path: str,
    project_name: str,
    owner: str,
    repo: str,
) -> dict:
    """Determine where to submit the PR.

    Resolution order:
    1. submit_to_repository in projects.yaml config
    2. Auto-detect fork parent via gh
    3. Fall back to issue's owner/repo

    Returns dict with 'repo' (owner/repo) and 'is_fork' (bool).
    """
    from app.projects_config import load_projects_config, get_project_submit_to_repository

    koan_root = os.environ.get("KOAN_ROOT", "")
    if koan_root:
        config = load_projects_config(koan_root)
        if config:
            submit_cfg = get_project_submit_to_repository(config, project_name)
            if submit_cfg.get("repo"):
                return {"repo": submit_cfg["repo"], "is_fork": True}

    parent = detect_parent_repo(project_path)
    if parent:
        return {"repo": parent, "is_fork": True}

    return {"repo": f"{owner}/{repo}", "is_fork": False}


def submit_draft_pr(
    project_path: str,
    project_name: str,
    owner: str,
    repo: str,
    issue_number: str,
    pr_title: str,
    pr_body: str,
    issue_url: Optional[str] = None,
) -> Optional[str]:
    """Push branch and create a draft PR.

    Handles the full PR submission pipeline:
    1. Check current branch (skip if on main/master)
    2. Check for existing PR on this branch
    3. Push branch to origin
    4. Resolve submit target (config, fork detection, fallback)
    5. Create draft PR
    6. Comment on the issue (if issue_url provided)

    Args:
        project_path: Local path to the project repository.
        project_name: Project name for config lookups.
        owner: GitHub repo owner (from the issue URL).
        repo: GitHub repo name (from the issue URL).
        issue_number: Issue number for the cross-link comment.
        pr_title: Full PR title string (caller builds it).
        pr_body: Full PR body markdown (caller builds it).
        issue_url: Optional issue URL for the cross-link comment.

    Returns:
        PR URL on success, or None on failure.
    """
    branch = get_current_branch(project_path)
    if branch in ("main", "master"):
        logger.info("On %s — skipping PR creation", branch)
        return None

    # Check for existing PR on this branch
    try:
        existing = run_gh(
            "pr", "list", "--head", branch, "--json", "url", "--jq", ".[0].url",
            cwd=project_path, timeout=15,
        ).strip()
        if existing:
            logger.info("PR already exists: %s", existing)
            return existing
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        logger.debug("No existing PR found (or check failed): %s", e)

    # Verify we have commits to submit
    base_branch = resolve_base_branch(project_name, project_path)
    commits = get_commit_subjects(project_path, base_branch=base_branch)
    if not commits:
        logger.info("No commits on branch — skipping PR creation")
        return None

    # Push branch
    try:
        run_git_strict(
            "push", "-u", "origin", branch,
            cwd=project_path, timeout=120,
        )
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        logger.warning("Failed to push branch: %s", e)
        return None

    # Resolve where to submit
    target = resolve_submit_target(project_path, project_name, owner, repo)

    pr_kwargs = {
        "title": pr_title,
        "body": pr_body,
        "draft": True,
        "cwd": project_path,
    }

    if target["is_fork"]:
        pr_kwargs["repo"] = target["repo"]
        fork_owner = get_fork_owner(project_path)
        if fork_owner:
            pr_kwargs["head"] = f"{fork_owner}:{branch}"

    try:
        pr_url = pr_create(**pr_kwargs)
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        logger.warning("Failed to create PR: %s", e)
        return None

    # Comment on the issue with the PR link
    if issue_url:
        try:
            run_gh(
                "issue", "comment", str(issue_number),
                "--repo", f"{owner}/{repo}",
                "--body", f"Draft PR submitted: {pr_url}",
                cwd=project_path, timeout=15,
            )
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            logger.debug("Failed to comment on issue: %s", e)

    return pr_url
