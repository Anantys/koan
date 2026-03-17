"""
Kōan -- Lint Gate

Runs project-specific lint commands on changed files as a quality gate.
Configured per-project via projects.yaml `lint` key.

The gate runs post-mission (before auto-merge) and optionally in skill
runners (rebase, recreate). It validates that Claude's changes pass
the project's linting rules before code reaches the main branch.

Usage:
    from app.lint_gate import run_lint_gate

    result = run_lint_gate(project_path, project_name, instance_dir)
    if not result.passed:
        # block auto-merge, log to journal, etc.
"""

import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.projects_config import (
    get_project_config,
    load_projects_config,
    resolve_base_branch,
)


@dataclass
class LintResult:
    """Result of a lint gate run."""

    passed: bool
    output: str
    command: str


def get_project_lint_config(config: dict, project_name: str) -> dict:
    """Get lint gate config for a project from projects.yaml.

    Returns a dict with keys: enabled, command, timeout, blocking.
    Falls back to defaults section, then sensible defaults.
    """
    project_cfg = get_project_config(config, project_name)
    lint = project_cfg.get("lint", {}) or {}

    return {
        "enabled": lint.get("enabled", False),
        "command": lint.get("command", ""),
        "timeout": lint.get("timeout", 60),
        "blocking": lint.get("blocking", True),
    }


def _get_changed_files(project_path: str, base_branch: str) -> list:
    """Get list of changed files relative to base branch.

    Returns file paths relative to project root, filtered to only
    files that still exist (excludes deletions).
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=d",
             f"origin/{base_branch}...HEAD"],
            capture_output=True, text=True, timeout=30,
            cwd=project_path, stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            return []
        return [f for f in result.stdout.strip().splitlines() if f.strip()]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []


def _expand_command(command: str, files: list) -> str:
    """Expand {files} placeholder in command with space-separated file list.

    If {files} is not in the command, returns the command as-is.
    Caps file list to avoid command-line length limits (ARG_MAX).
    """
    if "{files}" not in command:
        return command

    # Cap at 100 files to avoid ARG_MAX issues
    capped = files[:100]
    files_str = " ".join(shlex.quote(f) for f in capped)
    return command.replace("{files}", files_str)


def run_lint_gate(
    project_path: str,
    project_name: str,
    instance_dir: str = "",
) -> Optional[LintResult]:
    """Run the lint gate for a project.

    Reads lint config from projects.yaml, gets changed files via git diff,
    and runs the configured lint command.

    Args:
        project_path: Path to the project directory.
        project_name: Project name (for config lookup).
        instance_dir: Path to instance directory (for journal writing).

    Returns:
        LintResult if lint was configured and ran, None if not configured
        or no changed files.
    """
    import os

    koan_root = os.environ.get("KOAN_ROOT", "")
    if not koan_root:
        return None

    config = load_projects_config(koan_root)
    if not config:
        return None

    lint_config = get_project_lint_config(config, project_name)
    if not lint_config["enabled"] or not lint_config["command"]:
        return None

    # Get changed files
    base_branch = resolve_base_branch(project_name, project_path)
    changed_files = _get_changed_files(project_path, base_branch)
    if not changed_files:
        return None  # Nothing to lint

    # Expand command with file list
    command = _expand_command(lint_config["command"], changed_files)
    timeout = lint_config["timeout"]

    # Run lint command
    try:
        result = subprocess.run(
            shlex.split(command),
            capture_output=True, text=True,
            timeout=timeout,
            cwd=project_path,
            stdin=subprocess.DEVNULL,
        )
        output = (result.stdout + result.stderr)[-3000:]
        passed = result.returncode == 0

        lint_result = LintResult(passed=passed, output=output, command=command)
    except subprocess.TimeoutExpired:
        lint_result = LintResult(
            passed=False,
            output=f"Lint command timed out after {timeout}s",
            command=command,
        )
    except FileNotFoundError:
        # Lint tool not installed — treat as warning, not failure
        print(
            f"[lint_gate] Lint command not found: {command}",
            file=sys.stderr,
        )
        return None
    except OSError as e:
        print(f"[lint_gate] Lint command failed to start: {e}", file=sys.stderr)
        return None

    # Write result to journal
    if instance_dir:
        _write_journal_entry(instance_dir, project_name, lint_result)

    return lint_result


def _write_journal_entry(
    instance_dir: str, project_name: str, result: LintResult
) -> None:
    """Write lint gate result to daily journal."""
    try:
        from app.journal import append_to_journal

        timestamp = datetime.now().strftime("%H:%M")
        status = "PASSED" if result.passed else "FAILED"
        entry = (
            f"\n## Lint Gate — {timestamp}\n\n"
            f"Command: `{result.command}`\n"
            f"Status: {status}\n"
        )
        if not result.passed and result.output:
            # Include truncated output for failures
            output_snippet = result.output[:500]
            entry += f"\n```\n{output_snippet}\n```\n"

        append_to_journal(Path(instance_dir), project_name, entry)
    except Exception as e:
        print(f"[lint_gate] Journal write failed: {e}", file=sys.stderr)
