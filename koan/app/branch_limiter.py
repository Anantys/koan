"""Branch saturation limiter — caps unreviewed work per project.

Counts "pending branches" as the union (deduplicated by branch name) of:
1. Local unmerged koan/* branches (via GitSync)
2. Open PR branches on GitHub (via gh CLI)

When the count reaches ``max_pending_branches``, the project is
considered branch-saturated: no new missions are picked up and
exploration is blocked until branches are reviewed/merged.

Provides:
- count_pending_branches(project_path, github_urls, author) -> int
- is_project_branch_saturated(config, project_name, ...) -> bool
"""

import logging
from typing import List, Set

log = logging.getLogger(__name__)


def _get_local_unmerged_branches(instance_dir: str, project_name: str,
                                  project_path: str) -> Set[str]:
    """Return set of local unmerged koan/* branch names."""
    try:
        from app.git_sync import GitSync
        sync = GitSync(instance_dir, project_name, project_path)
        return set(sync.get_unmerged_branches())
    except Exception as e:
        log.debug("Failed to get local unmerged branches for %s: %s",
                  project_name, e)
        return set()


def _get_open_pr_branches(github_urls: List[str], author: str) -> Set[str]:
    """Return set of branch names from open PRs across all GitHub URLs."""
    if not author or not github_urls:
        return set()

    from app.github import list_open_pr_branches

    pr_branches: Set[str] = set()
    for url in github_urls:
        try:
            branches = list_open_pr_branches(url, author)
            pr_branches.update(branches)
        except Exception as e:
            log.debug("Failed to list open PR branches for %s: %s", url, e)
    return pr_branches


def count_pending_branches(
    instance_dir: str,
    project_name: str,
    project_path: str,
    github_urls: List[str],
    author: str,
) -> int:
    """Count pending (unreviewed) branches for a project.

    Returns the size of the union of local unmerged branches and open
    PR branches, deduplicated by branch name.

    On GitHub API errors, falls back to local-only count.
    """
    local_branches = _get_local_unmerged_branches(
        instance_dir, project_name, project_path,
    )
    pr_branches = _get_open_pr_branches(github_urls, author)

    # Union: a branch with both a local copy and an open PR counts once
    return len(local_branches | pr_branches)


def is_project_branch_saturated(
    config: dict,
    project_name: str,
    instance_dir: str,
    project_path: str,
    github_urls: List[str],
    author: str,
) -> bool:
    """Check if a project has reached its max_pending_branches limit.

    Returns False if the limit is 0 (unlimited) or if the count is
    below the limit.
    """
    from app.projects_config import get_project_max_pending_branches

    limit = get_project_max_pending_branches(config, project_name)
    if limit == 0:
        return False

    count = count_pending_branches(
        instance_dir, project_name, project_path, github_urls, author,
    )
    if count >= limit:
        log.info(
            "Project '%s' branch-saturated (%d/%d pending branches)",
            project_name, count, limit,
        )
        return True
    return False
