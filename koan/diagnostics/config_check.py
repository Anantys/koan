"""
Kōan diagnostic — Configuration checks.

Validates config.yaml, projects.yaml, and soul.md.
Reuses existing config_validator and projects_config modules.
"""

from pathlib import Path
from typing import List

from diagnostics import CheckResult


def run(koan_root: str, instance_dir: str) -> List[CheckResult]:
    """Run configuration diagnostic checks."""
    results = []

    # --- config.yaml ---
    config_path = Path(instance_dir) / "config.yaml"
    if not config_path.exists():
        results.append(CheckResult(
            name="config_yaml",
            severity="error",
            message="config.yaml not found",
            hint=f"Create {config_path} (see instance.example/config.yaml)",
        ))
    else:
        try:
            from app.utils import load_config
            config = load_config()
            from app.config_validator import validate_config
            warnings = validate_config(config)
            if warnings:
                for key_path, msg in warnings:
                    results.append(CheckResult(
                        name=f"config_yaml_{key_path}",
                        severity="warn",
                        message=f"config.yaml: {msg}",
                    ))
            else:
                results.append(CheckResult(
                    name="config_yaml",
                    severity="ok",
                    message="config.yaml is valid",
                ))
        except Exception as e:
            results.append(CheckResult(
                name="config_yaml",
                severity="error",
                message=f"config.yaml could not be loaded: {e}",
                hint="Check YAML syntax in config.yaml",
            ))

    # --- projects.yaml ---
    try:
        from app.projects_config import load_projects_config, validate_project_paths

        config = load_projects_config(koan_root)
        if config is None:
            results.append(CheckResult(
                name="projects_yaml",
                severity="warn",
                message="projects.yaml not found",
                hint="Run /projects add or create projects.yaml manually",
            ))
        else:
            path_error = validate_project_paths(config)
            if path_error:
                results.append(CheckResult(
                    name="projects_yaml_paths",
                    severity="warn",
                    message=f"projects.yaml: {path_error}",
                ))
            else:
                projects = config.get("projects", {})
                count = len(projects) if projects else 0
                results.append(CheckResult(
                    name="projects_yaml",
                    severity="ok",
                    message=f"projects.yaml is valid ({count} project(s))",
                ))
    except Exception as e:
        results.append(CheckResult(
            name="projects_yaml",
            severity="error",
            message=f"projects.yaml error: {e}",
            hint="Check YAML syntax in projects.yaml",
        ))

    # --- soul.md ---
    soul_path = Path(instance_dir) / "soul.md"
    if not soul_path.exists():
        results.append(CheckResult(
            name="soul_md",
            severity="warn",
            message="soul.md not found",
            hint=f"Create {soul_path} to define agent personality",
        ))
    else:
        content = soul_path.read_text().strip()
        if not content:
            results.append(CheckResult(
                name="soul_md",
                severity="warn",
                message="soul.md is empty",
                hint="Add personality definition to soul.md",
            ))
        else:
            results.append(CheckResult(
                name="soul_md",
                severity="ok",
                message="soul.md exists",
            ))

    return results
