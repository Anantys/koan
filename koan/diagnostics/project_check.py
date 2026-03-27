"""
Kōan diagnostic — Project health checks.

Validates project paths, git repo status, and remote reachability.
Remote checks are behind the --full flag (slow).
"""

import subprocess
from pathlib import Path
from typing import List

from diagnostics import CheckResult


def run(koan_root: str, instance_dir: str, full: bool = False) -> List[CheckResult]:
    """Run project health diagnostic checks."""
    results = []

    # Load projects config
    try:
        from app.projects_config import load_projects_config, get_projects_from_config
        config = load_projects_config(koan_root)
    except Exception as e:
        results.append(CheckResult(
            name="projects_load",
            severity="error",
            message=f"Could not load projects config: {e}",
        ))
        return results

    if config is None:
        results.append(CheckResult(
            name="projects_config",
            severity="warn",
            message="No projects.yaml found",
            hint="Run /projects add to register projects",
        ))
        return results

    projects = get_projects_from_config(config)
    if not projects:
        results.append(CheckResult(
            name="projects_config",
            severity="warn",
            message="No projects configured in projects.yaml",
            hint="Run /projects add to register projects",
        ))
        return results

    for name, path in projects:
        project_path = Path(path)

        # Check path exists
        if not project_path.is_dir():
            results.append(CheckResult(
                name=f"project_{name}",
                severity="error",
                message=f"Project '{name}' path missing: {path}",
                hint="Update projects.yaml or recreate the directory",
            ))
            continue

        # Check it's a git repo
        git_dir = project_path / ".git"
        if not git_dir.exists():
            results.append(CheckResult(
                name=f"project_{name}",
                severity="error",
                message=f"Project '{name}' is not a git repo: {path}",
                hint="Initialize with 'git init' or re-clone",
            ))
            continue

        # Check for uncommitted changes on main (warn only)
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=path, capture_output=True, text=True, timeout=10,
            )
            dirty_count = len([l for l in result.stdout.splitlines() if l.strip()])
            if dirty_count > 0:
                results.append(CheckResult(
                    name=f"project_{name}",
                    severity="ok",
                    message=f"Project '{name}' ok ({dirty_count} uncommitted change(s))",
                ))
            else:
                results.append(CheckResult(
                    name=f"project_{name}",
                    severity="ok",
                    message=f"Project '{name}' ok (clean)",
                ))
        except Exception:
            results.append(CheckResult(
                name=f"project_{name}",
                severity="ok",
                message=f"Project '{name}' exists (git status unavailable)",
            ))

        # Remote reachability — only with --full
        if full:
            try:
                result = subprocess.run(
                    ["git", "ls-remote", "--exit-code", "--quiet", "origin"],
                    cwd=path, capture_output=True, text=True, timeout=15,
                )
                if result.returncode == 0:
                    results.append(CheckResult(
                        name=f"project_{name}_remote",
                        severity="ok",
                        message=f"Project '{name}' remote is reachable",
                    ))
                else:
                    results.append(CheckResult(
                        name=f"project_{name}_remote",
                        severity="warn",
                        message=f"Project '{name}' remote not reachable",
                        hint="Check git remote configuration and network",
                    ))
            except subprocess.TimeoutExpired:
                results.append(CheckResult(
                    name=f"project_{name}_remote",
                    severity="warn",
                    message=f"Project '{name}' remote check timed out",
                ))
            except Exception as e:
                results.append(CheckResult(
                    name=f"project_{name}_remote",
                    severity="warn",
                    message=f"Project '{name}' remote check failed: {e}",
                ))

    return results
