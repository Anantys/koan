"""Merged project registry — unifies workspace + projects.yaml.

Single source of truth for the project list. Combines auto-discovered
workspace projects with manually configured projects.yaml entries.

Resolution: projects.yaml entries take precedence over workspace entries
with the same name.
"""

import logging
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_MAX_PROJECTS = 50

# Thread-safe cache
_lock = threading.Lock()
_cached_projects: Optional[List[Tuple[str, str]]] = None
_cached_warnings: List[str] = []
_cached_root: Optional[str] = None
_cached_yaml_mtime: Optional[float] = None
_cached_workspace_mtime: Optional[float] = None
_github_url_cache: Dict[str, str] = {}
# ALL github URLs per workspace project (origin + upstream + others).
# Keyed by project name → list of "owner/repo" strings (lowercase).
# Used by notification filtering to match cross-owner repos (fork workflows).
_github_all_urls_cache: Dict[str, List[str]] = {}


def get_all_projects(koan_root: str) -> List[Tuple[str, str]]:
    """Return merged, deduplicated, sorted list of (name, path) tuples.

    Sources (in priority order):
    1. projects.yaml — explicit configuration always wins
    2. workspace/ — auto-discovered projects fill gaps

    Duplicates: yaml entry wins; a warning is emitted.
    Limit: 50 projects total across both sources.
    """
    with _lock:
        if _cached_projects is not None and _cached_root == koan_root:
            # Invalidate if projects.yaml or workspace/ changed on disk
            if not _is_yaml_stale(koan_root) and not _is_workspace_stale(koan_root):
                return list(_cached_projects)

    return refresh_projects(koan_root)


def _get_yaml_mtime(koan_root: str) -> Optional[float]:
    """Get projects.yaml mtime, or None if missing."""
    try:
        return (Path(koan_root) / "projects.yaml").stat().st_mtime
    except OSError:
        return None


def _is_yaml_stale(koan_root: str) -> bool:
    """Check if projects.yaml mtime differs from cached value.

    Must be called with _lock held.
    """
    return _get_yaml_mtime(koan_root) != _cached_yaml_mtime


def _get_workspace_mtime(koan_root: str) -> Optional[float]:
    """Get workspace/ directory mtime, or None if missing.

    The directory mtime changes whenever entries are added or removed,
    which is exactly when workspace project discovery needs to re-scan.
    """
    try:
        return (Path(koan_root) / "workspace").stat().st_mtime
    except OSError:
        return None


def _is_workspace_stale(koan_root: str) -> bool:
    """Check if workspace/ directory mtime differs from cached value.

    Must be called with _lock held.
    """
    return _get_workspace_mtime(koan_root) != _cached_workspace_mtime


def refresh_projects(koan_root: str) -> List[Tuple[str, str]]:
    """Re-scan workspace and reload projects.yaml. Updates cache.

    Call at startup and when /projects command is invoked.
    Returns the merged project list.
    """
    from app.workspace_discovery import discover_workspace_projects

    warnings = []

    # 1. Load yaml projects
    yaml_projects = _load_yaml_projects(koan_root, warnings)
    
    # 2. Discover workspace projects
    workspace_projects = discover_workspace_projects(koan_root)

    # 3. Merge and deduplicate
    merged_projects = _merge_projects(yaml_projects, workspace_projects, warnings)
    
    # 4. Sort and enforce limit
    result = _apply_project_limit(merged_projects, warnings)

    # 5. Update cache
    _update_cache(koan_root, result, warnings)

    return result


def _load_yaml_projects(koan_root: str, warnings: List[str]) -> List[Tuple[str, str]]:
    """Load projects from projects.yaml. Returns list of (name, path) tuples."""
    try:
        from app.projects_config import load_projects_config, get_projects_from_config
        config = load_projects_config(koan_root)
        if config is not None:
            return get_projects_from_config(config)
    except Exception as e:
        warnings.append(f"⚠️ Cannot load projects.yaml: {e}")
    return []


def _merge_projects(
    yaml_projects: List[Tuple[str, str]],
    workspace_projects: List[Tuple[str, str]],
    warnings: List[str]
) -> Dict[str, str]:
    """Merge yaml and workspace projects, with yaml taking precedence.
    
    Returns a dict mapping project name to path.
    """
    # Build lookup for yaml projects (case-insensitive)
    yaml_by_name = {name.lower(): (name, path) for name, path in yaml_projects}
    
    # Start with yaml projects
    merged = {name: path for name, path in yaml_projects}
    
    # Add workspace projects if not already in yaml
    for ws_name, ws_path in workspace_projects:
        yaml_entry = yaml_by_name.get(ws_name.lower())
        if yaml_entry:
            # Duplicate: yaml wins; only warn when paths actually differ
            yaml_name, yaml_path = yaml_entry
            if yaml_path != ws_path:
                warnings.append(
                    f"⚠️ Duplicate project '{ws_name}': "
                    f"using {yaml_path} (yaml) instead of {ws_path} (workspace)"
                )
        else:
            # New workspace project
            merged[ws_name] = ws_path
    
    return merged


def _apply_project_limit(
    projects: Dict[str, str],
    warnings: List[str]
) -> List[Tuple[str, str]]:
    """Sort projects and enforce the limit. Returns list of (name, path) tuples."""
    sorted_projects = sorted(projects.items(), key=lambda x: x[0].lower())
    
    if len(sorted_projects) > _MAX_PROJECTS:
        warnings.append(
            f"⚠️ {len(sorted_projects)} projects found, "
            f"limit is {_MAX_PROJECTS}. Keeping first {_MAX_PROJECTS} alphabetically."
        )
        sorted_projects = sorted_projects[:_MAX_PROJECTS]
    
    return sorted_projects


def _update_cache(koan_root: str, projects: List[Tuple[str, str]], warnings: List[str]) -> None:
    """Update the thread-safe cache with new project list and warnings."""
    with _lock:
        global _cached_projects, _cached_warnings, _cached_root
        global _cached_yaml_mtime, _cached_workspace_mtime
        _cached_projects = list(projects)
        _cached_warnings = list(warnings)
        _cached_root = koan_root
        _cached_yaml_mtime = _get_yaml_mtime(koan_root)
        _cached_workspace_mtime = _get_workspace_mtime(koan_root)


def get_warnings() -> List[str]:
    """Return warnings from last refresh (duplicates, limit, errors)."""
    with _lock:
        return list(_cached_warnings)


def invalidate_cache() -> None:
    """Clear the project cache. Next get_all_projects() call will re-scan."""
    with _lock:
        global _cached_projects, _cached_warnings, _cached_root
        global _cached_yaml_mtime, _cached_workspace_mtime
        _cached_projects = None
        _cached_warnings = []
        _cached_root = None
        _cached_yaml_mtime = None
        _cached_workspace_mtime = None


def get_github_url_cache() -> Dict[str, str]:
    """Return the in-memory github_url cache (project_name -> url)."""
    with _lock:
        return dict(_github_url_cache)


def set_github_url(project_name: str, url: str) -> None:
    """Cache a github_url for a project (workspace or yaml)."""
    with _lock:
        _github_url_cache[project_name] = url


def get_github_url(project_name: str) -> Optional[str]:
    """Get cached github_url for a project, or None."""
    with _lock:
        return _github_url_cache.get(project_name)


def get_all_github_urls(project_name: str) -> List[str]:
    """Get ALL cached github URLs for a workspace project.

    Returns a list of "owner/repo" strings (lowercase) from all git remotes.
    For fork workflows, this includes both origin (fork) and upstream.
    Returns empty list if not cached.
    """
    with _lock:
        return list(_github_all_urls_cache.get(project_name, []))


def set_all_github_urls(project_name: str, urls: List[str]) -> None:
    """Cache ALL github URLs for a workspace project."""
    with _lock:
        _github_all_urls_cache[project_name] = list(urls)


def get_all_github_urls_cache() -> Dict[str, List[str]]:
    """Return the full all-URLs cache (project_name -> list of urls)."""
    with _lock:
        return {k: list(v) for k, v in _github_all_urls_cache.items()}


def clear_github_url_cache() -> None:
    """Clear the github_url memory cache."""
    with _lock:
        _github_url_cache.clear()
        _github_all_urls_cache.clear()


def get_yaml_project_names(koan_root: str) -> set:
    """Get set of project names that have paths in projects.yaml.
    
    Used to distinguish yaml projects from workspace-only projects.
    Returns empty set if projects.yaml doesn't exist or fails to load.
    """
    from app.projects_config import load_projects_config
    
    try:
        config = load_projects_config(koan_root)
        if not config:
            return set()
        
        return {
            name for name, proj in config.get("projects", {}).items()
            if isinstance(proj, dict) and proj.get("path")
        }
    except (ValueError, OSError):
        return set()


def populate_workspace_github_urls(koan_root: str) -> int:
    """Populate github_url cache for workspace projects by scanning git remotes.

    Only processes projects that are not in projects.yaml (workspace-only projects).
    Caches both the primary URL (origin) and ALL remote URLs for cross-owner
    matching (fork workflows where notifications come from the upstream repo).
    Returns the number of URLs discovered.
    """
    from app.utils import get_all_github_remotes, get_github_remote

    # Get yaml project names
    yaml_project_names = get_yaml_project_names(koan_root)

    # Scan workspace projects for github URLs
    projects = get_all_projects(koan_root)
    discovered = 0

    for name, path in projects:
        # Only process workspace projects (not in yaml)
        if name in yaml_project_names:
            continue

        # Skip if already cached
        if get_github_url(name):
            continue

        # Skip non-git directories to avoid timeout
        if not (Path(path) / ".git").exists():
            continue

        # Discover and cache primary URL
        gh_url = get_github_remote(path)
        if gh_url:
            set_github_url(name, gh_url)
            discovered += 1

        # Cache ALL remotes for cross-owner notification matching
        all_urls = get_all_github_remotes(path)
        if all_urls:
            set_all_github_urls(name, all_urls)

    return discovered
