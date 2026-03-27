#!/usr/bin/env python3
"""
Kōan -- Shared utilities

Core shared utilities used across modules:
- load_dotenv: .env file loading
- load_config: config.yaml loading
- parse_project: [project:name] / [projet:name] tag extraction
- atomic_write: crash-safe file writes
- insert_pending_mission: append mission to missions.md pending section
- modify_missions_file: locked read-modify-write on missions.md
- get_known_projects / resolve_project_path: project registry
- append_to_outbox: outbox file appending

Configuration, journal, and telegram history functions have been
extracted to dedicated modules (config.py, journal.py, conversation_history.py).
Backward-compatible re-exports are provided below.
"""

import fcntl
import os
import re
import subprocess
import sys
import tempfile
import threading
import yaml
from pathlib import Path
from typing import List, Optional, Tuple


if "KOAN_ROOT" not in os.environ:
    raise SystemExit("KOAN_ROOT environment variable is not set. Run via 'make run' or 'make awake'.")
KOAN_ROOT = Path(os.environ["KOAN_ROOT"])

# Pre-compiled regex for project tag extraction (accepts both [project:X] and [projet:X])
_PROJECT_TAG_RE = re.compile(r'\[projec?t:([a-zA-Z0-9_-]+)\]')
_PROJECT_TAG_STRIP_RE = re.compile(r'\[projec?t:[a-zA-Z0-9_-]+\]\s*')

_MISSIONS_DEFAULT = "# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n"
_MISSIONS_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Core utilities (stay here)
# ---------------------------------------------------------------------------

def load_dotenv():
    """Load .env file from the project root, stripping quotes from values.

    Uses os.environ.setdefault so existing env vars are not overwritten.
    """
    env_path = KOAN_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def load_config() -> dict:
    """Load configuration from instance/config.yaml.

    Returns the full config dict, or empty dict if file doesn't exist.
    """
    config_path = KOAN_ROOT / "instance" / "config.yaml"
    if not config_path.exists():
        return {}
    try:
        with open(config_path, "r") as f:
            return yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError) as e:
        print(f"[utils] Error loading config: {e}")
        return {}


# Track whether we've already logged the deprecation warning
_cli_provider_warned = False


def get_cli_provider_env() -> str:
    """Get CLI provider from environment variables.

    Reads KOAN_CLI_PROVIDER (primary) with fallback to CLI_PROVIDER (deprecated).
    Logs a deprecation warning once per process if the fallback is used.

    Returns:
        The environment variable value (lowercase, stripped), or empty string if neither is set.
    """
    global _cli_provider_warned

    # Primary: KOAN_CLI_PROVIDER
    value = os.environ.get("KOAN_CLI_PROVIDER", "").strip().lower()
    if value:
        return value

    # Fallback: CLI_PROVIDER (deprecated)
    fallback = os.environ.get("CLI_PROVIDER", "").strip().lower()
    if fallback:
        if not _cli_provider_warned:
            print("[utils] Warning: CLI_PROVIDER is deprecated. Use KOAN_CLI_PROVIDER instead.")
            _cli_provider_warned = True
        return fallback

    return ""


def parse_project(text: str) -> Tuple[Optional[str], str]:
    """Extract [project:name] or [projet:name] from text.

    Returns (project_name, cleaned_text) where cleaned_text has the tag removed.
    Returns (None, text) if no tag found.
    """
    match = _PROJECT_TAG_RE.search(text)
    if match:
        project = match.group(1)
        cleaned = _PROJECT_TAG_STRIP_RE.sub('', text).strip()
        return project, cleaned
    return None, text


def detect_project_from_text(text: str) -> Tuple[Optional[str], str]:
    """Detect project name from the first word of text.

    If the first word matches a known project name (case-insensitive),
    returns (project_name, remaining_text). Otherwise returns (None, text).
    """
    parts = text.strip().split(None, 1)
    if not parts:
        return None, text

    first_word = parts[0].lower()
    known = get_known_projects()
    project_names = {name.lower(): name for name, _path in known}

    if first_word in project_names:
        remaining = parts[1].strip() if len(parts) > 1 else ""
        return project_names[first_word], remaining

    return None, text


# Pre-compiled regex for GitHub remote URL parsing (SSH and HTTPS)
_GITHUB_REMOTE_RE = re.compile(r'github\.com[:/]([^/]+)/([^/\s.]+?)(?:\.git)?$')


def get_github_remote(project_path: str) -> Optional[str]:
    """Extract owner/repo from a project's git remote.

    Tries 'origin' first, falls back to 'upstream'.
    Returns "owner/repo" as a normalized lowercase string, or None on failure.
    """
    for remote in ("origin", "upstream"):
        try:
            result = subprocess.run(
                ["git", "remote", "get-url", remote],
                stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=5,
                cwd=project_path,
            )
            if result.returncode != 0:
                continue
            url = result.stdout.strip()
            match = _GITHUB_REMOTE_RE.search(url)
            if match:
                owner = match.group(1).lower()
                repo = match.group(2).lower()
                return f"{owner}/{repo}"
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            continue
    return None


def get_all_github_remotes(project_path: str) -> List[str]:
    """Extract owner/repo from ALL git remotes in a project.

    Returns a list of "owner/repo" strings (normalized lowercase) for every
    remote that points to GitHub.  Useful for matching a GitHub URL against
    a local project that may have both an origin (fork) and an upstream.
    """
    remotes: List[str] = []
    try:
        result = subprocess.run(
            ["git", "remote"],
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=5,
            cwd=project_path,
        )
        if result.returncode != 0:
            return remotes
        remote_names = result.stdout.strip().splitlines()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return remotes

    for remote in remote_names:
        try:
            result = subprocess.run(
                ["git", "remote", "get-url", remote.strip()],
                stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=5,
                cwd=project_path,
            )
            if result.returncode != 0:
                continue
            url = result.stdout.strip()
            match = _GITHUB_REMOTE_RE.search(url)
            if match:
                owner = match.group(1).lower()
                repo = match.group(2).lower()
                slug = f"{owner}/{repo}"
                if slug not in remotes:
                    remotes.append(slug)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            continue
    return remotes


def atomic_write(path: Path, content: str):
    """Write content to a file atomically using write-to-temp + rename.

    Prevents data loss if the process crashes mid-write. Uses an exclusive
    lock on the temp file to serialize concurrent writers.
    """
    dir_path = path.parent
    fd, tmp = tempfile.mkstemp(dir=str(dir_path), prefix=".koan-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def truncate_text(text: str, max_chars: int) -> str:
    """Truncate text with indicator."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...(truncated)"


def _locked_missions_rw(missions_path: Path, transform):
    """Read-modify-write missions.md with crash-safe atomic writes.

    Uses a separate lock file for cross-process synchronization so that
    the data file can be replaced atomically via temp + rename. A process
    crash between truncate() and write() previously risked leaving
    missions.md empty; this pattern eliminates that window entirely.

    Args:
        missions_path: Path to missions.md
        transform: Callable(content: str) -> str that returns modified content.

    Returns the transformed content.
    """
    lock_path = missions_path.with_suffix(".lock")
    missions_path = Path(missions_path)

    with _MISSIONS_LOCK:
        # Ensure parent directory exists (for first-run or test scenarios)
        missions_path.parent.mkdir(parents=True, exist_ok=True)

        with open(lock_path, "w") as lock_f:
            fcntl.flock(lock_f, fcntl.LOCK_EX)
            try:
                # Read current content (or default if missing/empty)
                if missions_path.exists():
                    content = missions_path.read_text(encoding="utf-8")
                else:
                    content = ""
                if not content.strip():
                    content = _MISSIONS_DEFAULT

                new_content = transform(content)

                # Atomic write: temp file + rename (same dir = same filesystem)
                fd, tmp = tempfile.mkstemp(
                    dir=str(missions_path.parent), prefix=".missions-",
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write(new_content)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(tmp, str(missions_path))
                except BaseException:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    raise
            finally:
                fcntl.flock(lock_f, fcntl.LOCK_UN)

    return new_content


def insert_pending_mission(missions_path: Path, entry: str, *, urgent: bool = False):
    """Insert a mission entry into the pending section of missions.md.

    By default, inserts at the bottom of the pending section (FIFO queue).
    When urgent=True, inserts at the top (next to be picked up).

    Uses file locking for the entire read-modify-write cycle to prevent
    TOCTOU race conditions between awake.py and dashboard.py.
    Creates the file with default structure if it doesn't exist.
    """
    from app.missions import insert_mission

    _locked_missions_rw(
        missions_path,
        lambda content: insert_mission(content, entry, urgent=urgent),
    )


def modify_missions_file(missions_path: Path, transform):
    """Apply a transform function to missions.md content with file locking.

    Args:
        missions_path: Path to missions.md
        transform: Callable(content: str) -> str that returns modified content.

    Returns the transformed content.
    """
    return _locked_missions_rw(missions_path, transform)


def get_known_projects() -> list:
    """Return sorted list of (name, path) tuples.

    Resolution order:
    1. Merged registry: projects.yaml + workspace/ (if either exists)
    2. KOAN_PROJECTS env var (fallback)

    Returns empty list if none is configured.
    """
    # 1. Try merged registry (projects.yaml + workspace/)
    try:
        from app.projects_merged import get_all_projects
        result = get_all_projects(str(KOAN_ROOT))
        if result:
            return result
    except Exception as e:
        print(f"[utils] Merged project registry failed: {e}", file=sys.stderr)

    # 2. Try projects.yaml alone (fallback if merged module fails)
    try:
        from app.projects_config import load_projects_config, get_projects_from_config
        config = load_projects_config(str(KOAN_ROOT))
        if config is not None:
            return get_projects_from_config(config)
    except Exception as e:
        print(f"[utils] projects.yaml loader failed: {e}", file=sys.stderr)

    # 3. KOAN_PROJECTS env var
    projects_str = os.environ.get("KOAN_PROJECTS", "")
    if projects_str:
        result = []
        for pair in projects_str.split(";"):
            pair = pair.strip()
            if ":" in pair:
                name, path = pair.split(":", 1)
                result.append((name.strip(), path.strip()))
        return sorted(result, key=lambda x: x[0].lower())

    return []


def is_known_project(name: str) -> bool:
    """Check if a name matches a known project (case-insensitive)."""
    try:
        return name.lower() in {n.lower() for n, _ in get_known_projects()}
    except Exception as e:
        print(f"[utils] is_known_project error: {e}", file=sys.stderr)
        return False


def project_name_for_path(project_path: str) -> str:
    """Get the project name for a given local path.

    Checks known projects first; falls back to the directory basename.
    """
    for name, path in get_known_projects():
        if path == project_path:
            return name
    return Path(project_path).name


def _find_partial_name_candidates(
    repo_lower: str, projects: list
) -> list:
    """Find projects whose name/basename partially matches the repo name.

    Catches aliased clones: e.g. repo "perl-convert-asn1" with local dir
    "convert-asn1".  Matches when one name is a dash-separated suffix of
    the other.

    Returns a list of (name, path) tuples — candidates to validate via remote.
    """
    candidates = []
    for name, path in projects:
        name_lower = name.lower()
        basename_lower = Path(path).name.lower()
        for local in (name_lower, basename_lower):
            if local == repo_lower:
                continue  # Already handled by exact-match steps
            # repo name ends with -<local> (e.g., "perl-convert-asn1" ends with "-convert-asn1")
            if repo_lower.endswith(f"-{local}") or repo_lower.endswith(f"_{local}"):
                candidates.append((name, path))
                break
            # local name ends with -<repo> (e.g., local "perl-convert-asn1" for repo "convert-asn1")
            if local.endswith(f"-{repo_lower}") or local.endswith(f"_{repo_lower}"):
                candidates.append((name, path))
                break
    return candidates


def _persist_and_cache_remotes(
    name: str, path: str, all_remotes: list, projects: list
) -> None:
    """Persist discovered github remotes to yaml and in-memory cache."""
    primary = get_github_remote(path)
    try:
        from app.projects_config import load_projects_config, save_projects_config
        config = load_projects_config(str(KOAN_ROOT))
        if config and name in config.get("projects", {}):
            proj = config["projects"][name]
            if isinstance(proj, dict) and proj.get("path"):
                if primary and not proj.get("github_url"):
                    proj["github_url"] = primary
                proj["github_urls"] = all_remotes
                save_projects_config(str(KOAN_ROOT), config)
    except Exception as e:
        print(f"[utils] Failed to persist github_urls for {name}: {e}", file=sys.stderr)
    if primary:
        try:
            from app.projects_merged import set_github_url
            set_github_url(name, primary)
        except Exception as e:
            print(f"[utils] Failed to cache github_url for {name}: {e}", file=sys.stderr)


def resolve_project_path(repo_name: str, owner: Optional[str] = None) -> Optional[str]:
    """Find local project path matching a repository name.

    Tries in order:
    1. GitHub URL match (if owner provided): check github_url and github_urls
       in projects.yaml — github_urls includes ALL remotes (origin, upstream, etc.)
       so cross-owner matches work on the fast path
    2. Exact match on project name (case-insensitive)
    3. Match on directory basename (case-insensitive)
    3b. Partial name match + remote validation: when the repo was cloned with
        a different local name (e.g., perl-Convert-ASN1 → Convert-ASN1), check
        if a project name/basename is a suffix of the repo name (or vice versa)
        and validate via git remotes.
    4. Auto-discover from ALL git remotes (if owner provided): subprocess
       fallback for projects not yet populated by ensure_github_urls()
    5. Fallback to single project if only one configured
    6. Cross-owner repo-name match (if owner provided): match the repo name
       against the repo component of configured github_url/github_urls.
       E.g. "sukria/koan" matches a project with github_url "atoomic/koan".
       Only used when exactly one project matches (avoids ambiguity).
    """
    projects = get_known_projects()
    target = f"{owner}/{repo_name}".lower() if owner else None

    # 1. GitHub URL match via projects.yaml and in-memory cache
    if target:
        try:
            from app.projects_config import load_projects_config
            config = load_projects_config(str(KOAN_ROOT))
            if config:
                for name, project in config.get("projects", {}).items():
                    if isinstance(project, dict):
                        # Check primary github_url
                        gh_url = project.get("github_url", "")
                        if gh_url and gh_url.lower() == target:
                            path = project.get("path")
                            if path:
                                return path
                        # Check all remotes (cross-owner: fork origin + upstream)
                        gh_urls = project.get("github_urls", [])
                        if target in (u.lower() for u in gh_urls):
                            path = project.get("path")
                            if path:
                                return path
        except Exception as e:
            print(f"[utils] GitHub URL match via projects.yaml failed: {e}", file=sys.stderr)
        # Also check in-memory github_url caches (workspace projects)
        try:
            from app.projects_merged import get_all_github_urls_cache, get_github_url_cache
            # Check primary URL cache
            for proj_name, gh_url in get_github_url_cache().items():
                if gh_url.lower() == target:
                    for name, path in projects:
                        if name == proj_name:
                            return path
            # Check all-URLs cache (covers forks with upstream remotes)
            for proj_name, urls in get_all_github_urls_cache().items():
                if target in (u.lower() for u in urls):
                    for name, path in projects:
                        if name == proj_name:
                            return path
        except Exception as e:
            print(f"[utils] GitHub URL cache lookup failed: {e}", file=sys.stderr)

    # 2. Exact match on project name
    for name, path in projects:
        if name.lower() == repo_name.lower():
            return path

    # 3. Match on directory basename
    for name, path in projects:
        if Path(path).name.lower() == repo_name.lower():
            return path

    # 3b. Partial name match + remote validation
    #     Handles aliased clones: repo "perl-Convert-ASN1" cloned as "Convert-ASN1".
    #     Checks if a project name/basename is a suffix of the repo name (or vice
    #     versa) separated by a dash, then validates via git remote.
    if target:
        repo_lower = repo_name.lower()
        candidates = _find_partial_name_candidates(repo_lower, projects)
        for _cname, cpath in candidates:
            all_remotes = get_all_github_remotes(cpath)
            if target in all_remotes:
                _persist_and_cache_remotes(_cname, cpath, all_remotes, projects)
                return cpath

    # 4. Auto-discover from ALL git remotes (origin, upstream, etc.)
    #    This catches cross-owner matches: e.g. local origin is atoomic/koan
    #    but the PR URL points to sukria/koan (the upstream remote).
    if target:
        for name, path in projects:
            all_remotes = get_all_github_remotes(path)
            if target in all_remotes:
                _persist_and_cache_remotes(name, path, all_remotes, projects)
                return path

    # 5. Fallback to single project (skip when owner-specific lookup found nothing)
    if not owner and len(projects) == 1:
        return projects[0][1]

    # 6. Cross-owner repo-name match: e.g. "sukria/koan" matches a project
    #    whose github_url is "atoomic/koan" — same repo, different owner.
    #    Only used when exactly one project matches to avoid ambiguity.
    if target:
        repo_lower = repo_name.lower()
        try:
            from app.projects_config import load_projects_config
            config = load_projects_config(str(KOAN_ROOT))
            if config:
                candidates = []
                for pname, project in config.get("projects", {}).items():
                    if not isinstance(project, dict):
                        continue
                    all_urls = []
                    gh_url = project.get("github_url")
                    if gh_url:
                        all_urls.append(gh_url)
                    all_urls.extend(project.get("github_urls", []))
                    for u in all_urls:
                        if "/" in u and u.rsplit("/", 1)[1].lower() == repo_lower:
                            path = project.get("path")
                            if path and path not in candidates:
                                candidates.append(path)
                            break
                if len(candidates) == 1:
                    return candidates[0]
        except Exception as e:
            print(f"[utils] Cross-owner repo-name match failed: {e}", file=sys.stderr)

    return None


def append_to_outbox(outbox_path: Path, content: str, priority=None):
    """Append content to outbox.md with file locking.

    Safe to call from run.py via: python3 -c "from app.utils import append_to_outbox; ..."
    or from Python directly.

    Args:
        outbox_path: Path to outbox.md
        content: Message content to append
        priority: Optional NotificationPriority — when provided, prepends a
                  [priority:name] header so flush_outbox() can parse and apply
                  priority-based filtering. Legacy callers omitting priority
                  default to ACTION in flush_outbox().
    """
    if priority is not None:
        # Import here to avoid circular imports (utils is imported at module level
        # by many modules including notify.py which defines NotificationPriority)
        try:
            from app.notify import NotificationPriority
            if isinstance(priority, NotificationPriority):
                content = f"[priority:{priority.name.lower()}]\n{content}"
        except ImportError:
            pass  # If import fails, write without header (treated as action)

    with open(outbox_path, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(content)
            f.flush()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Backward-compatible re-exports
# ---------------------------------------------------------------------------
# These preserve existing `from app.utils import X` patterns.
# New code should import from the dedicated modules directly.

from app.config import (  # noqa: E402, F401
    get_chat_tools,
    get_mission_tools,
    get_allowed_tools,
    get_tools_description,
    get_model_config,
    get_start_on_pause,
    get_max_runs,
    get_interval_seconds,
    get_fast_reply_model,
    get_branch_prefix,
    get_contemplative_chance,
    build_claude_flags,
    get_claude_flags_for_role,
    get_cli_binary_for_shell,
    get_cli_provider_name,
    get_tool_flags_for_shell,
    get_output_flags_for_shell,
    get_auto_merge_config,
)

from app.journal import (  # noqa: E402, F401
    get_journal_file,
    read_all_journals,
    get_latest_journal,
    append_to_journal,
)

from app.conversation_history import (  # noqa: E402, F401
    save_conversation_message as save_telegram_message,
    load_recent_history as load_recent_telegram_history,
    format_conversation_history,
    compact_history as compact_telegram_history,
)
