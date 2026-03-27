"""Project configuration loader — reads projects.yaml.

Provides:
- load_projects_config(koan_root) -> dict: Load and validate projects.yaml
- get_projects_from_config(config) -> list[tuple[str, str]]: Extract (name, path) tuples
- get_project_config(config, name) -> dict: Get merged defaults + project overrides
- get_project_auto_merge(config, name) -> dict: Get auto-merge config for a project
- get_project_cli_provider(config, name) -> str: Get CLI provider for a project
- get_project_models(config, name) -> dict: Get model overrides for a project
- get_project_tools(config, name) -> dict: Get tool restrictions for a project
- get_project_exploration(config, name) -> bool: Get exploration flag for a project
- get_project_max_open_prs(config, name) -> int: Get max open PRs limit for a project
- get_project_github_authorized_users(config, name) -> list: Get GitHub authorized users

File location: projects.yaml at KOAN_ROOT (next to .env).
"""

import sys
from pathlib import Path
from typing import List, Optional, Tuple

import yaml


def load_projects_config(koan_root: str) -> Optional[dict]:
    """Load projects.yaml from KOAN_ROOT.

    Returns the parsed config dict, or None if file doesn't exist.
    Raises ValueError on invalid YAML or schema violations.
    """
    config_path = Path(koan_root) / "projects.yaml"
    if not config_path.exists():
        return None

    try:
        with open(config_path, "r") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML in projects.yaml: {e}")

    if data is None:
        return None

    if not isinstance(data, dict):
        raise ValueError("projects.yaml must be a YAML mapping (dict)")

    _validate_config(data)
    return data


def _validate_config(config: dict) -> None:
    """Validate the structure of the projects config.

    Raises ValueError on validation failures.
    """
    # defaults section is optional, must be dict if present
    defaults = config.get("defaults")
    if defaults is not None and not isinstance(defaults, dict):
        raise ValueError("'defaults' must be a mapping")

    # projects section is optional — missing means 0 projects (workspace provides them)
    projects = config.get("projects")
    if projects is None:
        return

    if not isinstance(projects, dict):
        raise ValueError("'projects' must be a mapping of project_name -> config")

    if len(projects) > 50:
        raise ValueError(f"Max 50 projects allowed. You have {len(projects)}.")

    # Check for case-insensitive duplicates
    seen_lower = {}
    for name in projects.keys():
        lower = name.lower()
        if lower in seen_lower:
            raise ValueError(
                f"Duplicate project name (case-insensitive): "
                f"'{seen_lower[lower]}' and '{name}'"
            )
        seen_lower[lower] = name

    for name, project in projects.items():
        if not isinstance(name, str):
            raise ValueError(f"Project name must be a string, got: {type(name).__name__}")

        if project is None:
            # Allow empty project entries (workspace override with no settings)
            continue

        if not isinstance(project, dict):
            raise ValueError(f"Project '{name}' must be a mapping, got: {type(project).__name__}")

        # path is optional — workspace projects don't need it in yaml
        path = project.get("path")
        if path is not None and (not isinstance(path, str) or not path.strip()):
            raise ValueError(f"Project '{name}' has invalid path: {path!r}")


def validate_project_paths(config: dict) -> Optional[str]:
    """Check that all project paths exist on disk.

    Returns an error message if any path is missing, or None if all valid.
    Projects without a path (workspace-only overrides) are skipped.
    Separated from _validate_config() so tests can skip filesystem checks.
    """
    projects = config.get("projects", {})
    for name, project in projects.items():
        if project is None:
            continue
        path = project.get("path", "")
        if not path:
            continue  # Workspace project — no path to validate
        if not Path(path).is_dir():
            return f"Project '{name}' path does not exist: {path}"
    return None


def get_projects_from_config(config: dict) -> List[Tuple[str, str]]:
    """Extract sorted (name, path) tuples from config.

    Same format as get_known_projects() returns — enables drop-in replacement.
    Projects without a path (workspace-only overrides) are skipped.
    """
    projects = config.get("projects", {})
    result = []
    for name, proj in projects.items():
        if proj is None:
            continue
        path = proj.get("path", "").strip()
        if path:
            result.append((name, path))
    return sorted(result, key=lambda x: x[0].lower())


def _find_project_entry(projects: dict, project_name: str) -> dict:
    """Case-insensitive lookup of a project entry in the projects dict."""
    # Fast path: exact match
    entry = projects.get(project_name)
    if entry is not None:
        return entry
    # Slow path: case-insensitive scan
    lower = project_name.lower()
    for key, value in projects.items():
        if key.lower() == lower:
            return value
    return {}


def get_project_config(config: dict, project_name: str) -> dict:
    """Get merged config for a project (defaults + project overrides).

    Deep-merges per-section: project-level keys override default-level keys.
    Unknown sections are passed through as-is.
    Project name lookup is case-insensitive.
    """
    defaults = config.get("defaults", {}) or {}
    project = _find_project_entry(config.get("projects", {}), project_name) or {}

    merged = {}
    # Start with all default keys
    for key, value in defaults.items():
        if isinstance(value, dict):
            # Deep merge dicts (one level)
            project_value = project.get(key, {}) or {}
            merged[key] = {**value, **project_value}
        else:
            merged[key] = project.get(key, value)

    # Add project-only keys not in defaults
    for key, value in project.items():
        if key == "path":
            continue  # path is structural, not a setting
        if key not in merged:
            merged[key] = value

    return merged


def get_project_auto_merge(config: dict, project_name: str) -> dict:
    """Get auto-merge config for a project from projects.yaml.

    Returns a dict with keys: enabled, base_branch, strategy, rules.
    Falls back to defaults section, then sensible defaults.
    """
    project_cfg = get_project_config(config, project_name)
    am = project_cfg.get("git_auto_merge", {}) or {}

    return {
        "enabled": am.get("enabled", False),
        "base_branch": am.get("base_branch", "main"),
        "strategy": am.get("strategy", "squash"),
        "rules": am.get("rules", []),
    }


def resolve_base_branch(
    project_name: str, project_path: Optional[str] = None
) -> str:
    """Resolve the base branch for a project.

    Resolution order:
    1. Explicit per-project base_branch in projects.yaml
    2. Auto-detection from the remote's default branch (if project_path given)
    3. Defaults section base_branch from projects.yaml
    4. Hardcoded fallback: 'main'

    Safe to call when KOAN_ROOT is unset or config is missing — returns 'main'.
    """
    import os

    config_branch = "main"
    project_explicit = False

    try:
        koan_root = os.environ.get("KOAN_ROOT", "")
        if koan_root:
            config = load_projects_config(koan_root)
            if config:
                am = get_project_auto_merge(config, project_name)
                config_branch = am.get("base_branch", "main")

                # Check if the project explicitly sets base_branch
                projects = config.get("projects", {}) or {}
                proj_cfg = _find_project_entry(projects, project_name) or {}
                proj_am = proj_cfg.get("git_auto_merge", {}) or {}
                if proj_am.get("base_branch"):
                    project_explicit = True
    except (ValueError, OSError, KeyError):
        pass

    # If project explicitly sets the branch, trust it
    if project_explicit:
        return config_branch

    # Try auto-detection from the remote
    if project_path:
        try:
            from app.git_prep import detect_remote_default_branch, get_upstream_remote

            koan_root = os.environ.get("KOAN_ROOT", "")
            remote = "origin"
            if koan_root:
                remote = get_upstream_remote(project_path, project_name, koan_root)
            detected = detect_remote_default_branch(remote, project_path)
            if detected:
                return detected
        except Exception as e:
            print(f"[projects_config] default branch detection failed for {project_name} (non-fatal): {e}", file=sys.stderr)

    return config_branch


def get_project_cli_provider(config: dict, project_name: str) -> str:
    """Get CLI provider for a project from projects.yaml.

    Returns the provider name ("claude", "copilot", "local") or empty string
    if not configured (meaning: use the global provider).

    Note: Data accessor only — the provider resolution in cli_provider.py
    does not yet call this. Per-project provider switching requires changes
    to get_provider() to accept a project_name parameter.
    """
    project_cfg = get_project_config(config, project_name)
    return str(project_cfg.get("cli_provider", "")).strip().lower()


def get_project_models(config: dict, project_name: str) -> dict:
    """Get model overrides for a project from projects.yaml.

    Returns a dict with model role keys (mission, chat, lightweight, etc.).
    Only includes keys that are explicitly set — caller should merge with
    global defaults.
    """
    project_cfg = get_project_config(config, project_name)
    models = project_cfg.get("models", {})
    if not isinstance(models, dict):
        return {}
    return models


def get_project_tools(config: dict, project_name: str) -> dict:
    """Get tool restrictions for a project from projects.yaml.

    Returns a dict with keys: mission, chat (lists of tool names).
    Only includes keys that are explicitly set — caller should merge with
    global defaults.
    """
    project_cfg = get_project_config(config, project_name)
    tools = project_cfg.get("tools", {})
    if not isinstance(tools, dict):
        return {}
    return tools


def get_project_exploration(config: dict, project_name: str) -> bool:
    """Get exploration flag for a project from projects.yaml.

    Controls whether autonomous exploration (contemplative sessions and
    free-form autonomous work) is enabled for a project. When False, the
    agent only works on the project when explicit missions are queued.

    Returns True by default (exploration enabled).
    """
    project_cfg = get_project_config(config, project_name)
    value = project_cfg.get("exploration", True)

    # Handle string values like "false", "no", "0"
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "no", "0", "")

    return bool(value)


def get_project_max_open_prs(config: dict, project_name: str) -> int:
    """Get max open PRs limit for a project from projects.yaml.

    Controls the maximum number of open PRs allowed before autonomous
    exploration is paused for this project. When the limit is reached,
    the agent only works on explicit missions for the project.

    Returns 0 by default (unlimited).
    """
    project_cfg = get_project_config(config, project_name)
    value = project_cfg.get("max_open_prs", 0)

    # Coerce to int; invalid values map to 0 (unlimited)
    try:
        result = int(value)
    except (TypeError, ValueError):
        return 0

    # Negative or zero → unlimited
    return result if result > 0 else 0


def get_project_github_authorized_users(config: dict, project_name: str) -> list:
    """Get GitHub authorized users for a project from projects.yaml.

    Per-project github.authorized_users completely replaces global list.
    Returns the list of authorized GitHub usernames, or ["*"] for wildcard.
    Returns empty list if not configured.
    """
    project_cfg = get_project_config(config, project_name)
    github = project_cfg.get("github", {}) or {}
    users = github.get("authorized_users", [])
    return users if isinstance(users, list) else []


def get_project_github_reply_authorized_users(config: dict, project_name: str) -> Optional[list]:
    """Get GitHub reply_authorized_users for a project from projects.yaml.

    Per-project github.reply_authorized_users completely replaces global list.
    Returns the list of authorized GitHub usernames, or ["*"] for wildcard.
    Returns None if not configured (meaning: fall back to global config.yaml).
    """
    project_cfg = get_project_config(config, project_name)
    github = project_cfg.get("github", {}) or {}
    users = github.get("reply_authorized_users")
    if users is None:
        return None
    return users if isinstance(users, list) else None


def get_project_github_natural_language(config: dict, project_name: str) -> Optional[bool]:
    """Get GitHub natural_language setting for a project from projects.yaml.

    Per-project github.natural_language overrides the global setting.
    Returns True/False if explicitly set, or None if not configured
    (meaning: fall back to global config.yaml).
    """
    project_cfg = get_project_config(config, project_name)
    github = project_cfg.get("github", {}) or {}
    value = github.get("natural_language")
    if value is None:
        return None
    return bool(value)


def get_project_submit_to_repository(config: dict, project_name: str) -> dict:
    """Get submit_to_repository config for a project from projects.yaml.

    Controls where PRs are submitted for this project, especially for forks.
    Returns a dict with keys: repo (owner/repo), remote (git remote name).
    Returns empty dict if not configured.
    """
    project_cfg = get_project_config(config, project_name)
    value = project_cfg.get("submit_to_repository", {})
    if not isinstance(value, dict):
        return {}
    result = {}
    if value.get("repo"):
        result["repo"] = str(value["repo"])
    if value.get("remote"):
        result["remote"] = str(value["remote"])
    return result


def save_projects_config(koan_root: str, config: dict) -> None:
    """Write config back to projects.yaml atomically, preserving comments.

    Uses ruamel.yaml to round-trip the existing file so that user comments
    and formatting are kept intact. Falls back to plain pyyaml if ruamel
    is unavailable.
    """
    from app.utils import atomic_write

    config_path = Path(koan_root) / "projects.yaml"

    try:
        from ruamel.yaml import YAML
        import io

        ry = YAML()
        ry.preserve_quotes = True

        # Load existing file to preserve its comments
        existing = None
        if config_path.exists():
            with open(config_path, "r") as f:
                raw = f.read()
            if raw.strip():
                existing = ry.load(raw)

        if existing is not None and isinstance(existing, dict):
            _deep_merge_yaml(existing, config)
            data = existing
        else:
            data = config

        stream = io.StringIO()
        ry.dump(data, stream)
        content = stream.getvalue()

        # Add header only for brand-new files / non-dict existing content
        if (existing is None or not isinstance(existing, dict)) and not content.startswith("#"):
            header = (
                "# projects.yaml — Project configuration for Kōan\n"
                "# Auto-managed — manual edits are preserved.\n\n"
            )
            content = header + content

        atomic_write(config_path, content)
        return
    except ImportError:
        pass

    # Fallback: plain pyyaml (loses comments)
    header = (
        "# projects.yaml — Project configuration for Kōan\n"
        "# Auto-managed — manual edits are preserved.\n\n"
    )
    content = header + yaml.dump(config, default_flow_style=False, sort_keys=False)
    atomic_write(config_path, content)


def _deep_merge_yaml(target, source):
    """Recursively merge source dict into target, preserving target's comments.

    - Existing keys are updated in-place (comments on those keys survive).
    - New keys from source are added.
    - Keys removed from source are deleted from target.
    """
    # Update / add keys from source
    for key, value in source.items():
        if (
            key in target
            and isinstance(target[key], dict)
            and isinstance(value, dict)
        ):
            _deep_merge_yaml(target[key], value)
        else:
            target[key] = value

    # Remove keys not in source
    for key in list(target.keys()):
        if key not in source:
            del target[key]


def ensure_github_urls(koan_root: str) -> List[str]:
    """Populate missing github_url fields in projects.yaml from git remotes.

    Iterates all projects, calls get_github_remote() on any project without
    a github_url field, and saves the discovered URL back to projects.yaml.

    Returns a list of log messages for discovered URLs.
    Does NOT overwrite existing github_url values.
    """
    config = load_projects_config(koan_root)
    if config is None:
        return []

    projects = config.get("projects", {})
    if not projects:
        return []

    from app.utils import get_all_github_remotes, get_github_remote

    messages = []
    modified = False

    for name, project in projects.items():
        if not isinstance(project, dict):
            continue

        path = project.get("path", "")
        if not path or not Path(path).is_dir():
            continue

        # Populate primary github_url if missing
        if not project.get("github_url"):
            github_url = get_github_remote(path)
            if github_url:
                project["github_url"] = github_url
                messages.append(f"Discovered github_url for '{name}': {github_url}")
                modified = True

        # Always refresh github_urls (all remotes) for cross-owner resolution
        all_urls = get_all_github_remotes(path)
        if all_urls and set(all_urls) != set(project.get("github_urls", [])):
            project["github_urls"] = all_urls
            modified = True

    if modified:
        try:
            save_projects_config(koan_root, config)
        except OSError as e:
            messages.append(f"Warning: could not save projects.yaml: {e}")

    return messages
