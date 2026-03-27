"""PR tracking for the dashboard.

Fetches open PRs across all configured projects using the ``gh`` CLI wrapper,
with a 5-minute TTL in-memory cache.  Each project's PRs are fetched via
``gh pr list --json`` and aggregated into a single response.
"""

import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

from app.github import get_gh_username, run_gh
from app.projects_config import (
    _find_project_entry,
    get_project_auto_merge,
    get_project_config,
    get_projects_from_config,
    load_projects_config,
)


# Fields requested from gh pr list
_PR_FIELDS = (
    "number,title,author,headRefName,isDraft,url,"
    "createdAt,reviewDecision,statusCheckRollup,state"
)

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_pr_cache: Dict[str, tuple] = {}  # project_name -> (data, timestamp)
_pr_cache_lock = threading.Lock()
_PR_CACHE_TTL = 300  # 5 minutes


def _invalidate_cache(project: Optional[str] = None) -> None:
    """Clear cached PR data.  If *project* is given, clear only that entry."""
    with _pr_cache_lock:
        if project:
            _pr_cache.pop(project, None)
        else:
            _pr_cache.clear()


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def fetch_project_prs(
    project_name: str,
    project_path: str,
    github_url: str,
    author_filter: str = "",
) -> List[dict]:
    """Fetch open PRs for a single project via ``gh pr list``.

    Returns a list of PR dicts, or an empty list on any error.
    """
    args = [
        "pr", "list",
        "--repo", github_url,
        "--state", "open",
        "--json", _PR_FIELDS,
        "--limit", "20",
    ]
    if author_filter:
        args.extend(["--author", author_filter])

    try:
        output = run_gh(*args, cwd=project_path, timeout=20)
        prs = json.loads(output)
        if not isinstance(prs, list):
            return []
        # Attach project name to each PR for frontend grouping
        for pr in prs:
            pr["project"] = project_name
        return prs
    except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError,
            OSError, TypeError):
        return []


def fetch_all_prs(
    koan_root: str,
    project_filter: str = "",
    author_only: bool = True,
) -> dict:
    """Fetch open PRs for all configured projects.

    Args:
        koan_root: Path to KOAN_ROOT.
        project_filter: If set, fetch PRs for this project only.
        author_only: If True, filter to PRs authored by the Kōan bot user.

    Returns:
        Dict with keys: ``prs`` (list), ``error`` (str or None),
        ``stale`` (bool — True if returning cached data after a fetch failure).
    """
    config = load_projects_config(koan_root)
    if config is None:
        return {"prs": [], "error": "No projects configured", "stale": False}

    projects = get_projects_from_config(config)
    if not projects:
        return {"prs": [], "error": "No projects configured", "stale": False}

    if project_filter:
        projects = [(n, p) for n, p in projects if n == project_filter]
        if not projects:
            return {"prs": [], "error": None, "stale": False}

    author = ""
    if author_only:
        try:
            author = get_gh_username()
        except Exception as e:
            print(f"[pr_tracker] failed to get gh username: {e}", file=sys.stderr)
            author = ""

    all_prs: List[dict] = []
    had_errors = False

    def _fetch_one(name: str, path: str) -> List[dict]:
        now = time.monotonic()
        with _pr_cache_lock:
            cached = _pr_cache.get(name)
            if cached and (now - cached[1]) < _PR_CACHE_TTL:
                return cached[0]

        proj_cfg = get_project_config(config, name)
        github_url = proj_cfg.get("github_url", "")
        if not github_url:
            return []

        prs = fetch_project_prs(name, path, github_url, author_filter=author)
        with _pr_cache_lock:
            _pr_cache[name] = (prs, now)
        return prs

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {
            pool.submit(_fetch_one, name, path): name
            for name, path in projects
        }
        for future in as_completed(futures):
            try:
                prs = future.result()
                all_prs.extend(prs)
            except Exception as e:
                print(f"[pr_tracker] error fetching PRs: {e}", file=sys.stderr)
                had_errors = True

    # Sort by creation date descending (newest first)
    all_prs.sort(key=lambda pr: pr.get("createdAt", ""), reverse=True)

    return {
        "prs": all_prs,
        "error": None,
        "stale": had_errors and bool(all_prs),
    }


# ---------------------------------------------------------------------------
# CI checks (lazy per-PR)
# ---------------------------------------------------------------------------

def fetch_pr_checks(
    project_name: str,
    pr_number: int,
    koan_root: str,
) -> List[dict]:
    """Fetch CI check results for a single PR.

    Returns a list of check dicts with keys: name, state, conclusion.
    """
    config = load_projects_config(koan_root)
    if config is None:
        return []

    proj_cfg = get_project_config(config, project_name)
    project_path = (_find_project_entry(config.get("projects", {}), project_name) or {}).get("path", "")
    github_url = proj_cfg.get("github_url", "")
    if not project_path or not github_url:
        return []

    try:
        output = run_gh(
            "pr", "checks", str(pr_number),
            "--repo", github_url,
            "--json", "name,state,conclusion",
            cwd=project_path, timeout=15,
        )
        checks = json.loads(output)
        return checks if isinstance(checks, list) else []
    except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError,
            OSError, TypeError):
        return []


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

def merge_pr(
    project_name: str,
    pr_number: int,
    koan_root: str,
) -> dict:
    """Merge a PR if auto-merge is enabled for the project.

    Returns dict with keys: ``ok`` (bool), ``error`` (str or None),
    ``url`` (str — the merge result URL).
    """
    _VALID_STRATEGIES = {"squash", "merge", "rebase"}

    config = load_projects_config(koan_root)
    if config is None:
        return {"ok": False, "error": "No projects config", "url": ""}

    am_cfg = get_project_auto_merge(config, project_name)
    if not am_cfg.get("enabled"):
        return {"ok": False, "error": "Auto-merge is disabled for this project", "url": ""}

    strategy = am_cfg.get("strategy", "squash")
    if strategy not in _VALID_STRATEGIES:
        return {"ok": False, "error": f"Invalid merge strategy: {strategy}", "url": ""}

    proj_cfg = get_project_config(config, project_name)
    project_path = (_find_project_entry(config.get("projects", {}), project_name) or {}).get("path", "")
    github_url = proj_cfg.get("github_url", "")
    if not project_path or not github_url:
        return {"ok": False, "error": "Project path or GitHub URL not configured", "url": ""}

    try:
        output = run_gh(
            "pr", "merge", str(pr_number),
            f"--{strategy}",
            "--repo", github_url,
            cwd=project_path, timeout=30,
        )
        _invalidate_cache(project_name)
        return {"ok": True, "error": None, "url": output}
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as e:
        return {"ok": False, "error": str(e), "url": ""}
