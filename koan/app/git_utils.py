"""
Kōan -- Shared git command helpers.

Centralizes subprocess-based git invocation used across the codebase.
Replaces 5 separate run_git() implementations with two unified functions:

- run_git(): Returns (returncode, stdout, stderr) tuple. Never raises.
- run_git_strict(): Returns stdout string. Raises RuntimeError on failure.
"""

import os
import subprocess
from typing import Dict, List, Optional, Tuple


def run_git(
    *args: str,
    cwd: str = None,
    timeout: int = 30,
    env: Optional[Dict[str, str]] = None,
) -> Tuple[int, str, str]:
    """Run a git command and return (returncode, stdout, stderr).

    Args:
        *args: Git subcommand and arguments (e.g. "status", "--porcelain").
        cwd: Working directory for the git command.
        timeout: Command timeout in seconds (default: 30).
        env: Optional extra environment variables, merged on top of os.environ.

    Returns:
        (returncode, stdout, stderr) tuple. Never raises on git failures.
    """
    try:
        run_env = None
        if env:
            run_env = {**os.environ, **env}
        result = subprocess.run(
            ["git"] + list(args),
            stdin=subprocess.DEVNULL,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=run_env,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", "Git command timed out"
    except Exception as e:
        return 1, "", str(e)


def run_git_strict(
    *args: str,
    cwd: str = None,
    timeout: int = 60,
) -> str:
    """Run a git command, raise RuntimeError on failure.

    Args:
        *args: Git subcommand and arguments (e.g. "fetch", "origin", "main").
        cwd: Working directory for the git command.
        timeout: Command timeout in seconds (default: 60).

    Returns:
        Stripped stdout on success.

    Raises:
        RuntimeError: If git exits with non-zero status.
    """
    result = subprocess.run(
        ["git"] + list(args),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
    )
    if result.returncode != 0:
        cmd_str = " ".join(["git"] + list(args))
        raise RuntimeError(f"git failed: {cmd_str} — {result.stderr[:200]}")
    return result.stdout.strip()


def get_current_branch(cwd: str = None, default: str = "main") -> str:
    """Return the current git branch name.

    Args:
        cwd: Working directory for the git command.
        default: Value to return when branch detection fails.

    Returns:
        Branch name, or *default* on any error.
    """
    try:
        return run_git_strict("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd)
    except (RuntimeError, OSError, subprocess.SubprocessError):
        return default


def get_commit_subjects(
    cwd: str,
    base_branch: str = "main",
    branch: str = "HEAD",
) -> List[str]:
    """Return commit subject lines between *base_branch* and *branch*.

    Args:
        cwd: Working directory for the git command.
        base_branch: The base ref (e.g. ``"main"``).
        branch: The tip ref (default ``"HEAD"``).

    Returns:
        List of subject strings, empty list on failure.
    """
    rc, stdout, _ = run_git(
        "log", f"{base_branch}..{branch}", "--format=%s", cwd=cwd,
    )
    if rc != 0:
        return []
    return [s for s in stdout.splitlines() if s.strip()]


def ordered_remotes(preferred: Optional[str] = None) -> List[str]:
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
