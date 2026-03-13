"""Kōan — Git worktree lifecycle management.

Manages isolated git worktrees for parallel agent sessions:
- create_worktree(): create an isolated working directory with its own branch
- remove_worktree(): clean up worktree and associated state
- list_worktrees(): enumerate active worktrees
- cleanup_stale_worktrees(): prune worktrees whose sessions are gone
- git_retry(): retry wrapper for git commands that hit lock contention

Worktrees are stored under <project>/.worktrees/<session-id>/ to keep
them project-relative and easy to clean up. Each worktree gets a unique
branch named <prefix>/session-<uuid>.
"""

import os
import random
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


# Git lock retry configuration (per m13v's production finding)
GIT_RETRY_MAX = 3
GIT_RETRY_MIN_DELAY = 0.1
GIT_RETRY_MAX_DELAY = 0.5

# Default worktree directory name (relative to project root)
WORKTREE_DIR = ".worktrees"


@dataclass
class WorktreeInfo:
    """Information about a single git worktree."""
    path: str
    branch: str
    session_id: str
    project_path: str
    commit: str = ""
    is_main: bool = False


def _get_branch_prefix() -> str:
    """Get the configured branch prefix (default: 'koan').

    Returns the prefix WITHOUT trailing slash (e.g., 'koan').
    """
    try:
        from app.config import get_branch_prefix
        prefix = get_branch_prefix()
        return prefix.rstrip("/")
    except Exception:
        return "koan"


def _worktrees_dir(project_path: str) -> Path:
    """Return the .worktrees directory for a project."""
    return Path(project_path) / WORKTREE_DIR


def git_retry(
    cmd: List[str],
    cwd: str,
    max_retries: int = GIT_RETRY_MAX,
    min_delay: float = GIT_RETRY_MIN_DELAY,
    max_delay: float = GIT_RETRY_MAX_DELAY,
) -> subprocess.CompletedProcess:
    """Run a git command with retry logic for lock contention.

    Concurrent git operations across worktrees sharing .git/objects can
    hit LOCK_EX errors. This wrapper retries with random jitter.

    Raises subprocess.CalledProcessError after all retries exhausted.
    """
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            return subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as e:
            last_error = e
            stderr = e.stderr or ""
            # Only retry on lock-related errors
            if "lock" not in stderr.lower() and "index.lock" not in stderr.lower():
                raise
            if attempt < max_retries:
                delay = random.uniform(min_delay, max_delay)
                time.sleep(delay)
    raise last_error  # type: ignore[misc]


def create_worktree(
    project_path: str,
    branch_name: str = "",
    session_id: str = "",
    base_branch: str = "main",
) -> WorktreeInfo:
    """Create a new git worktree for a parallel session.

    Args:
        project_path: Path to the main project repository.
        branch_name: Branch name for the worktree. Auto-generated if empty.
        session_id: Unique session identifier. Auto-generated if empty.
        base_branch: Branch to base the worktree on (default: main).

    Returns:
        WorktreeInfo with path, branch, and session details.

    Raises:
        subprocess.CalledProcessError: If git worktree add fails.
        FileExistsError: If worktree directory already exists.
    """
    if not session_id:
        session_id = uuid.uuid4().hex[:12]

    if not branch_name:
        prefix = _get_branch_prefix()
        branch_name = f"{prefix}/session-{session_id}"

    # Ensure .worktrees directory exists
    wt_base = _worktrees_dir(project_path)
    wt_base.mkdir(parents=True, exist_ok=True)

    # Ensure .worktrees is gitignored
    _ensure_gitignored(project_path)

    wt_path = wt_base / session_id
    if wt_path.exists():
        raise FileExistsError(f"Worktree path already exists: {wt_path}")

    # Determine the actual base branch/commit
    base_ref = _resolve_base_ref(project_path, base_branch)

    # Create the worktree with a new branch
    git_retry(
        ["git", "worktree", "add", "-b", branch_name, str(wt_path), base_ref],
        cwd=project_path,
    )

    # Get the HEAD commit of the new worktree
    commit = ""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(wt_path),
            capture_output=True,
            text=True,
            check=True,
        )
        commit = result.stdout.strip()
    except subprocess.CalledProcessError:
        pass

    # Copy project CLAUDE.md to worktree if it exists
    _copy_claude_md(project_path, str(wt_path))

    return WorktreeInfo(
        path=str(wt_path),
        branch=branch_name,
        session_id=session_id,
        project_path=project_path,
        commit=commit,
    )


def inject_worktree_claude_md(worktree_path: str, mission_text: str):
    """Append mission-specific context to the worktree's CLAUDE.md.

    Creates a section at the end of CLAUDE.md with the mission scope,
    so the agent knows what it's working on and that it's in a worktree.
    """
    claude_md = Path(worktree_path) / "CLAUDE.md"
    section = (
        "\n\n## Worktree Session Context\n\n"
        "This is an isolated worktree session. Changes here will be "
        "merged back via PR after completion.\n\n"
        f"**Current mission**: {mission_text}\n"
    )
    try:
        if claude_md.exists():
            existing = claude_md.read_text()
            claude_md.write_text(existing + section)
        else:
            claude_md.write_text(f"# CLAUDE.md\n{section}")
    except OSError:
        pass  # Non-fatal — agent can work without this


def remove_worktree(
    project_path: str,
    session_id: str = "",
    worktree_path: str = "",
    force: bool = False,
):
    """Remove a git worktree and clean up associated state.

    Args:
        project_path: Path to the main project repository.
        session_id: Session identifier (used to derive worktree path).
        worktree_path: Direct path to the worktree (alternative to session_id).
        force: If True, use --force flag for stubborn worktrees.

    Either session_id or worktree_path must be provided.
    """
    if not worktree_path and session_id:
        worktree_path = str(_worktrees_dir(project_path) / session_id)
    elif not worktree_path:
        raise ValueError("Either session_id or worktree_path must be provided")

    wt = Path(worktree_path)

    # Remove via git worktree remove (handles git bookkeeping)
    cmd = ["git", "worktree", "remove"]
    if force:
        cmd.append("--force")
    cmd.append(str(wt))

    try:
        subprocess.run(
            cmd,
            cwd=project_path,
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        # If git worktree remove fails, try manual cleanup
        if wt.exists():
            shutil.rmtree(str(wt), ignore_errors=True)

    # Prune any stale worktree references
    try:
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=project_path,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError:
        pass

    # Delete the branch if it still exists
    # (only session branches — don't delete user branches)
    if session_id:
        prefix = _get_branch_prefix()
        branch = f"{prefix}/session-{session_id}"
        try:
            subprocess.run(
                ["git", "branch", "-D", branch],
                cwd=project_path,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError:
            pass


def list_worktrees(project_path: str) -> List[WorktreeInfo]:
    """List all git worktrees for a project.

    Returns a list of WorktreeInfo, including the main worktree.
    """
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=project_path,
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        return []

    worktrees = []
    current: dict = {}

    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            if current:
                worktrees.append(_parse_worktree_entry(current, project_path))
            current = {"path": line[9:]}
        elif line.startswith("HEAD "):
            current["commit"] = line[5:]
        elif line.startswith("branch "):
            current["branch"] = line[7:]
        elif line == "bare":
            current["bare"] = True
        elif line == "":
            if current:
                worktrees.append(_parse_worktree_entry(current, project_path))
                current = {}

    if current:
        worktrees.append(_parse_worktree_entry(current, project_path))

    return worktrees


def cleanup_stale_worktrees(project_path: str, active_session_ids: Optional[List[str]] = None):
    """Remove worktrees whose sessions are no longer active.

    Args:
        project_path: Path to the main project repository.
        active_session_ids: List of currently active session IDs.
            If None, removes all worktrees in .worktrees/.
    """
    if active_session_ids is None:
        active_session_ids = []

    active_set = set(active_session_ids)
    wt_base = _worktrees_dir(project_path)
    if not wt_base.exists():
        return

    for entry in wt_base.iterdir():
        if not entry.is_dir():
            continue
        session_id = entry.name
        if session_id not in active_set:
            try:
                remove_worktree(
                    project_path,
                    session_id=session_id,
                    force=True,
                )
            except Exception:
                # Best-effort cleanup
                pass

    # Final prune
    try:
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=project_path,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError:
        pass


def setup_shared_deps(worktree_path: str, project_path: str, shared_deps: List[str]):
    """Symlink heavy dependency directories from main project to worktree.

    Args:
        worktree_path: Path to the worktree.
        project_path: Path to the main project.
        shared_deps: List of relative paths to symlink (e.g., ["node_modules", ".venv"]).
    """
    for dep in shared_deps:
        src = Path(project_path) / dep
        dst = Path(worktree_path) / dep
        if src.exists() and not dst.exists():
            try:
                # Ensure parent directory exists
                dst.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(str(src), str(dst))
            except OSError:
                pass  # Non-fatal — build may just take longer


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolve_base_ref(project_path: str, base_branch: str) -> str:
    """Resolve the base reference for worktree creation.

    Tries the specified base_branch, falls back to 'main', then 'master',
    then HEAD.
    """
    for ref in [base_branch, "main", "master", "HEAD"]:
        try:
            subprocess.run(
                ["git", "rev-parse", "--verify", ref],
                cwd=project_path,
                capture_output=True,
                text=True,
                check=True,
            )
            return ref
        except subprocess.CalledProcessError:
            continue
    return "HEAD"


def _copy_claude_md(project_path: str, worktree_path: str):
    """Copy CLAUDE.md from main project to worktree if it exists."""
    src = Path(project_path) / "CLAUDE.md"
    dst = Path(worktree_path) / "CLAUDE.md"
    if src.exists() and not dst.exists():
        try:
            shutil.copy2(str(src), str(dst))
        except OSError:
            pass


def _ensure_gitignored(project_path: str):
    """Ensure .worktrees/ is in .gitignore."""
    gitignore = Path(project_path) / ".gitignore"
    pattern = f"/{WORKTREE_DIR}/"
    try:
        if gitignore.exists():
            content = gitignore.read_text()
            if pattern in content or WORKTREE_DIR in content:
                return
            # Append to existing .gitignore
            if not content.endswith("\n"):
                content += "\n"
            content += f"{pattern}\n"
            gitignore.write_text(content)
        # Don't create .gitignore if it doesn't exist — that's the project's choice
    except OSError:
        pass


def _parse_worktree_entry(entry: dict, project_path: str) -> WorktreeInfo:
    """Parse a porcelain worktree entry into WorktreeInfo."""
    path = entry.get("path", "")
    branch = entry.get("branch", "").removeprefix("refs/heads/")
    commit = entry.get("commit", "")

    # Extract session_id from path (last component of .worktrees/<session-id>)
    session_id = ""
    wt_dir = str(_worktrees_dir(project_path))
    if path.startswith(wt_dir):
        remainder = path[len(wt_dir):].lstrip(os.sep)
        session_id = remainder.split(os.sep)[0] if remainder else ""

    # Detect if this is the main worktree
    is_main = os.path.normpath(path) == os.path.normpath(project_path)

    return WorktreeInfo(
        path=path,
        branch=branch,
        session_id=session_id,
        project_path=project_path,
        commit=commit,
        is_main=is_main,
    )
