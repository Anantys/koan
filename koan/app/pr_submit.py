"""Shared helpers for draft PR submission after skill execution.

Used by fix_runner.py and implement_runner.py to avoid duplicating
the post-execution PR submission pipeline (branch check, push,
fork detection, PR creation, tracker issue comment).
"""

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Callable, List, Optional

from app.github_url_parser import is_jira_url, search_jira_url
from app.git_utils import (
    get_commit_subjects as _git_get_commit_subjects,
    get_current_branch as _git_get_current_branch,
    run_git_strict,
)
from app.github import origin_repo, resolve_target_repo, run_gh, pr_create
from app.projects_config import resolve_base_branch
from app.tracker_comment_format import (
    build_pr_comment_failure,
    build_pr_comment_success,
)

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
    """Return the GitHub owner login of the PR head (the push target).

    Derived from the ``origin`` git remote — the branch is pushed there, so
    the cross-fork ``--head <owner>:<branch>`` must name the same owner.
    ``gh repo view`` is NOT used as the primary source: when an ``upstream``
    remote exists it resolves to the upstream/base repo and reports the
    *upstream* owner, which would point ``--head`` at a branch that doesn't
    exist on upstream and silently land the PR on the fork instead.
    """
    slug = origin_repo(project_path)
    if slug:
        return slug.split("/", 1)[0]
    # Fallback for setups without a parseable origin URL (e.g. gh-only auth).
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
    2. Auto-detect upstream (gh fork parent, then ``upstream`` git remote)
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

    # resolve_target_repo falls back to the `upstream` git remote when the
    # GitHub fork-parent lookup comes back empty (e.g. gh resolved the local
    # repo to the upstream itself, which reports no parent).
    upstream = resolve_target_repo(project_path)
    if upstream:
        return {"repo": upstream, "is_fork": True}

    return {"repo": f"{owner}/{repo}", "is_fork": False}


def _is_minimal_body(body: str) -> bool:
    """Return True if a PR body is too short or lacks structured sections.

    Bodies like "Closes #123." or "Fixes #456" are considered minimal —
    they contain no descriptive content beyond an issue reference.
    """
    if not body or not body.strip():
        return True
    stripped = body.strip()
    if len(stripped) < 80 and "##" not in stripped:
        return True
    return False


def _enrich_existing_pr(
    pr_number: int,
    pr_body: str,
    project_path: str,
    project_name: str = "",
    footer_enabled: bool = True,
    footer_model_key: str = "",
    footer_started_at=None,
) -> None:
    """Update an existing PR's body when the current body is minimal."""
    enriched = pr_body

    if footer_enabled:
        try:
            from app.pr_footer import append_koan_footer, build_pr_footer

            started_at = footer_started_at
            if started_at is None:
                raw = os.environ.get("KOAN_MISSION_STARTED_AT", "")
                try:
                    started_at = float(raw) if raw else None
                except ValueError:
                    started_at = None

            model_key = (
                footer_model_key
                or os.environ.get("KOAN_MISSION_MODEL_KEY", "")
                or "mission"
            )

            enriched = append_koan_footer(
                enriched,
                build_pr_footer(
                    project_name=project_name,
                    model_key=model_key,
                    project_path=project_path,
                    started_at=started_at,
                ),
            )
        except (ImportError, TypeError, ValueError, OSError) as e:
            logger.warning("Footer append failed during enrichment: %s", e)

    try:
        run_gh(
            "pr", "edit", str(pr_number),
            "--body", enriched,
            cwd=project_path, timeout=15,
        )
        logger.info("Enriched minimal PR #%d body", pr_number)
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        logger.warning("Failed to enrich PR #%d body: %s", pr_number, e)


def submit_draft_pr(
    project_path: str,
    project_name: str,
    owner: str,
    repo: str,
    issue_number: str,
    pr_title: str,
    pr_body: str,
    issue_url: Optional[str] = None,
    base_branch: Optional[str] = None,
    notify_fn: Optional[Callable[[str], None]] = None,
    skill_name: str = "",
    footer_enabled: bool = True,
    footer_model_key: str = "",
    footer_started_at: Optional[float] = None,
) -> Optional[str]:
    """Push branch and create a draft PR.

    Handles the full PR submission pipeline:
    1. Resolve the project's base branch; abort if HEAD is *on* that base
       branch (or on main/master) — committing there means the skill failed
       to create a feature branch and there's nothing diff-able to push.
    2. Check for existing PR on this branch
    3. Push branch to origin
    4. Resolve submit target (config, fork detection, fallback)
    5. Create draft PR
    6. Comment on the tracker issue (if issue_url provided)

    Args:
        project_path: Local path to the project repository.
        project_name: Project name for config lookups.
        owner: GitHub repo owner (from the issue URL).
        repo: GitHub repo name (from the issue URL).
        issue_number: Legacy issue identifier kept for caller compatibility.
        pr_title: Full PR title string (caller builds it).
        pr_body: Full PR body markdown (caller builds it).
        issue_url: Optional issue URL for the cross-link comment.
        base_branch: Optional target branch for the PR (e.g. "11.126").
            When set, overrides the auto-resolved base branch for both
            commit diffing and the PR's --base flag.
        notify_fn: Optional callable invoked with a one-line human-readable
            reason when PR submission fails (push error, gh error, no commits,
            HEAD landed on the base branch). Lets callers surface the failure
            to Telegram instead of leaving it in logs only.
        skill_name: Optional origin skill (e.g. ``"fix"``, ``"implement"``)
            included in Jira status comments.
        footer_enabled: Whether to normalize and append the Kōan attribution
            footer to the PR body.
        footer_model_key: Model slot to report in the footer. When omitted,
            falls back to ``KOAN_MISSION_MODEL_KEY`` then ``mission``.
        footer_started_at: Unix timestamp for elapsed-runtime reporting. When
            omitted, falls back to ``KOAN_MISSION_STARTED_AT`` if set.

    Returns:
        PR URL on success, or None on failure.
    """
    issue_provider = ""
    if issue_url:
        issue_provider = "jira" if is_jira_url(issue_url) else "github"

    def _post_issue_comment(body: str) -> None:
        if not issue_url or not body:
            return
        # Jira status comments go through the shared marker-based upsert so
        # repeated runs (e.g. stagnation retries) update one comment instead
        # of stacking duplicates, and share the same dedup key as the
        # end-of-mission Jira publisher.
        if issue_provider == "jira":
            match = search_jira_url(issue_url)
            if match:
                _, issue_key = match
                try:
                    from app.jira_outcome_publish import upsert_jira_comment

                    ok, reason = upsert_jira_comment(
                        issue_key, skill_name or "mission", body,
                    )
                    if not ok:
                        # The upsert declines to write when it cannot first read
                        # the existing comment, so a quiet failure here means no
                        # status was posted at all — say so.
                        logger.warning(
                            "Jira comment upsert failed for %s: %s", issue_key, reason,
                        )
                    return
                except Exception as e:
                    logger.debug("Failed to upsert Jira comment: %s", e)
                    return
        try:
            from app.issue_tracker import add_comment

            add_comment(
                issue_url,
                body,
                project_name=project_name,
                project_path=project_path,
            )
        except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as e:
            logger.debug("Failed to comment on issue: %s", e)

    branch = get_current_branch(project_path)

    # Resolve the effective base branch up-front: it gates both the "HEAD is
    # on the base" guard below AND the empty-diff check further down. Before
    # this fix, the guard hardcoded `("main", "master")` and let projects
    # configured with `base_branch: staging` (or any non-main/master base)
    # slip through, so Claude would commit straight onto staging and the
    # post-implementation PR submission silently no-op'd on the empty diff.
    effective_base = base_branch or resolve_base_branch(project_name, project_path)

    if branch == effective_base or branch in ("main", "master"):
        reason = (
            f"HEAD is on the base branch '{branch}' — the skill committed "
            "without first creating a feature branch, so there is nothing "
            "to push as a PR. The commits remain on your local base branch "
            "until you move them onto a feature branch manually."
        )
        logger.warning(reason)
        if notify_fn:
            notify_fn(f"❌ PR creation aborted: {reason}")
        if issue_provider == "jira":
            _post_issue_comment(
                build_pr_comment_failure(
                    "jira",
                    reason=reason,
                    branch=branch,
                    base_branch=effective_base,
                    skill_name=skill_name,
                ),
            )
        return None

    # Check for existing PR on this branch
    try:
        existing_raw = run_gh(
            "pr", "list", "--head", branch,
            "--json", "url,body,number",
            "--jq", ".[0]",
            cwd=project_path, timeout=15,
        ).strip()
        if existing_raw:
            try:
                existing_data = json.loads(existing_raw)
            except (json.JSONDecodeError, ValueError):
                logger.warning("Malformed gh pr list output: %s", existing_raw)
                existing_data = None
            if not isinstance(existing_data, dict) or "url" not in existing_data:
                existing_data = None
            if existing_data is None:
                raise RuntimeError("no usable PR data")
            existing_url = existing_data["url"]
            existing_body = existing_data.get("body", "")
            existing_number = existing_data.get("number")

            if pr_body and existing_number and _is_minimal_body(existing_body):
                _enrich_existing_pr(
                    existing_number, pr_body, project_path,
                    project_name=project_name,
                    footer_enabled=footer_enabled,
                    footer_model_key=footer_model_key,
                    footer_started_at=footer_started_at,
                )

            logger.info("PR already exists: %s", existing_url)
            return existing_url
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        logger.debug("No existing PR found (or check failed): %s", e)

    # Verify we have commits to submit
    commits = get_commit_subjects(project_path, base_branch=effective_base)
    if not commits:
        reason = (
            f"No commits found on '{branch}' relative to '{effective_base}'."
        )
        logger.info("%s — skipping PR creation", reason)
        if notify_fn:
            notify_fn(f"❌ PR creation skipped: {reason}")
        if issue_provider == "jira":
            _post_issue_comment(
                build_pr_comment_failure(
                    "jira",
                    reason=reason,
                    branch=branch,
                    base_branch=effective_base,
                    skill_name=skill_name,
                ),
            )
        return None

    # Push branch
    try:
        run_git_strict(
            "push", "-u", "origin", branch,
            cwd=project_path, timeout=120,
        )
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        reason = f"git push failed: {str(e)[:300]}"
        logger.warning(reason)
        if notify_fn:
            notify_fn(f"❌ PR creation failed — {reason}")
        if issue_provider == "jira":
            _post_issue_comment(
                build_pr_comment_failure(
                    "jira",
                    reason=reason,
                    branch=branch,
                    base_branch=effective_base,
                    skill_name=skill_name,
                ),
            )
        return None

    # Resolve where to submit
    target = resolve_submit_target(project_path, project_name, owner, repo)

    # Post the pending "koan/mission" commit status on the freshly-pushed head.
    # Best-effort: a failure here must never abort PR submission.
    try:
        from app.mission_status import on_branch_pushed

        koan_root = os.environ.get("KOAN_ROOT", "")
        if koan_root:
            instance_dir = os.path.join(koan_root, "instance")
            head_sha = run_git_strict(
                "rev-parse", "HEAD", cwd=project_path, timeout=15,
            ).strip()
            on_branch_pushed(
                instance_dir, project_name, target["repo"], branch, head_sha,
            )
    except Exception as e:
        logger.warning("running-indicator push hook skipped: %s", e)

    if footer_enabled:
        from app.pr_footer import append_koan_footer, build_pr_footer

        started_at = footer_started_at
        if started_at is None:
            started_at_raw = os.environ.get("KOAN_MISSION_STARTED_AT", "")
            try:
                started_at = float(started_at_raw) if started_at_raw else None
            except ValueError:
                started_at = None

        effective_model_key = (
            footer_model_key
            or os.environ.get("KOAN_MISSION_MODEL_KEY", "")
            or "mission"
        )

        pr_body = append_koan_footer(
            pr_body,
            build_pr_footer(
                project_name=project_name,
                model_key=effective_model_key,
                project_path=project_path,
                started_at=started_at,
            ),
        )

    pr_kwargs = {
        "title": pr_title,
        "body": pr_body,
        "draft": True,
        "cwd": project_path,
    }

    if base_branch:
        pr_kwargs["base"] = base_branch

    if target["is_fork"]:
        pr_kwargs["repo"] = target["repo"]
        fork_owner = get_fork_owner(project_path)
        if fork_owner:
            pr_kwargs["head"] = f"{fork_owner}:{branch}"

    try:
        pr_url = pr_create(**pr_kwargs)
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        reason = f"gh pr create failed: {str(e)[:300]}"
        logger.warning(reason)
        if notify_fn:
            notify_fn(f"❌ PR creation failed — {reason}")
        if issue_provider == "jira":
            _post_issue_comment(
                build_pr_comment_failure(
                    "jira",
                    reason=reason,
                    branch=branch,
                    base_branch=effective_base,
                    skill_name=skill_name,
                ),
            )
        return None

    # Comment on the source issue with the PR link. The issue may live in
    # GitHub or Jira, so use the provider-neutral issue tracker service.
    if issue_url:
        if issue_provider == "jira":
            _post_issue_comment(
                build_pr_comment_success(
                    "jira",
                    pr_url=pr_url,
                    pr_title=pr_title,
                    pr_body=pr_body,
                    skill_name=skill_name,
                    base_branch=base_branch,
                ),
            )
        else:
            _post_issue_comment(f"Draft PR submitted: {pr_url}")

    return pr_url
