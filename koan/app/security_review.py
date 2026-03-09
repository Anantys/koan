"""
Kōan -- Differential security review on mission diffs.

Analyzes git diffs for security-sensitive patterns before auto-merge:
- Blast radius calculation (files changed, modules affected)
- Risk classification based on security-sensitive patterns
- Journal logging of review results

Integration point: called from mission_runner.run_post_mission()
between reflection and auto-merge.
"""

import re
import subprocess
import sys
from fnmatch import fnmatch
from pathlib import Path
from typing import List, Optional, Tuple

# Security-sensitive file patterns (glob-style)
SENSITIVE_FILE_PATTERNS = [
    "*.env*",
    "*secret*",
    "*credential*",
    "*auth*",
    "*password*",
    "*token*",
    "*config.yaml",
    "*config.yml",
    "Dockerfile*",
    "docker-compose*",
    "*requirements*.txt",
    "pyproject.toml",
    "package.json",
    "package-lock.json",
    "Makefile",
    "*.sql",
    "*.pem",
    "*.key",
]

# Security-sensitive content patterns (regex)
SENSITIVE_CONTENT_PATTERNS = [
    (r"(?i)\beval\s*\(", "eval() usage"),
    (r"(?i)\bexec\s*\(", "exec() usage"),
    (r"(?i)subprocess\.(?:call|run|Popen)\s*\(.*shell\s*=\s*True", "shell=True subprocess"),
    (r"(?i)os\.system\s*\(", "os.system() usage"),
    (r"(?i)SQL.*(?:format|%s|\+)", "potential SQL injection"),
    (r"(?i)(?:api[_-]?key|secret[_-]?key|password)\s*=\s*['\"]", "hardcoded secret"),
    (r"(?i)disable.*(?:ssl|tls|verify|cert)", "SSL/TLS verification disabled"),
    (r"(?i)chmod\s+(?:777|666)", "overly permissive file permissions"),
    (r"(?i)--no-verify", "verification bypass"),
    (r"(?i)CORS.*\*|Access-Control-Allow-Origin.*\*", "wildcard CORS"),
    (r"(?i)(?:pickle|marshal)\.loads?\s*\(", "unsafe deserialization"),
    (r"(?i)\.innerHTML\s*=", "potential XSS via innerHTML"),
    (r"(?i)dangerouslySetInnerHTML", "React XSS risk"),
]

# Risk level thresholds (cumulative score → risk)
RISK_THRESHOLDS = {
    "critical": 20,
    "high": 12,
    "medium": 6,
    "low": 0,
}

# Severity ordering for threshold comparison
SEVERITY_ORDER = ["low", "medium", "high", "critical"]


def _run_git(project_path: str, *args: str, timeout: int = 30) -> str:
    """Run a git command and return stdout, or empty string on failure."""
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True, text=True,
            cwd=project_path, timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            return ""
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def get_diff_against_base(project_path: str, base_branch: str = "main") -> str:
    """Get unified diff of current branch against base branch.

    Tries upstream/<base>, origin/<base>, then <base> as fallbacks.
    """
    for ref in [f"upstream/{base_branch}", f"origin/{base_branch}", base_branch]:
        diff = _run_git(project_path, "diff", f"{ref}...HEAD")
        if diff:
            return diff
    return ""


def get_changed_files(project_path: str, base_branch: str = "main") -> List[str]:
    """Get list of files changed relative to base branch."""
    for ref in [f"upstream/{base_branch}", f"origin/{base_branch}", base_branch]:
        output = _run_git(project_path, "diff", "--name-only", f"{ref}...HEAD")
        if output:
            return [f for f in output.splitlines() if f.strip()]
    return []


def classify_file_sensitivity(filepath: str) -> bool:
    """Check if a file path matches any security-sensitive pattern."""
    basename = Path(filepath).name
    for pattern in SENSITIVE_FILE_PATTERNS:
        if fnmatch(basename, pattern) or fnmatch(filepath, pattern):
            return True
    return False


def scan_diff_for_patterns(diff_text: str) -> List[Tuple[str, str, str]]:
    """Scan a unified diff for security-sensitive content patterns.

    Only scans added lines (lines starting with '+', excluding '+++' headers).

    Returns:
        List of (pattern_description, matched_text, line) tuples.
    """
    findings = []
    for line in diff_text.splitlines():
        # Only scan added lines
        if not line.startswith("+") or line.startswith("+++"):
            continue

        content = line[1:]  # Strip the leading '+'
        for pattern_re, description in SENSITIVE_CONTENT_PATTERNS:
            match = re.search(pattern_re, content)
            if match:
                findings.append((description, match.group(0), content.strip()))
    return findings


def calculate_blast_radius(changed_files: List[str]) -> dict:
    """Calculate the blast radius of changes.

    Returns:
        Dict with keys: file_count, sensitive_files, sensitive_file_count,
        modules_affected, has_infra_changes, has_dependency_changes.
    """
    sensitive = [f for f in changed_files if classify_file_sensitivity(f)]

    # Count distinct top-level directories as "modules"
    modules = set()
    for f in changed_files:
        parts = Path(f).parts
        if len(parts) > 1:
            modules.add(parts[0])

    infra_patterns = ["Dockerfile*", "docker-compose*", "Makefile", "*.yml", "*.yaml"]
    has_infra = any(
        any(fnmatch(Path(f).name, p) for p in infra_patterns)
        for f in changed_files
    )

    dep_patterns = ["*requirements*.txt", "pyproject.toml", "package.json",
                    "package-lock.json", "Cargo.toml", "go.mod", "go.sum"]
    has_deps = any(
        any(fnmatch(Path(f).name, p) for p in dep_patterns)
        for f in changed_files
    )

    return {
        "file_count": len(changed_files),
        "sensitive_files": sensitive,
        "sensitive_file_count": len(sensitive),
        "modules_affected": sorted(modules),
        "has_infra_changes": has_infra,
        "has_dependency_changes": has_deps,
    }


def assess_risk_level(
    blast_radius: dict,
    content_findings: List[Tuple[str, str, str]],
) -> Tuple[str, int]:
    """Assess overall risk level from blast radius and content findings.

    Returns:
        (risk_level, score) where risk_level is one of:
        "low", "medium", "high", "critical".
    """
    score = 0

    # Blast radius scoring
    file_count = blast_radius.get("file_count", 0)
    if file_count > 20:
        score += 4
    elif file_count > 10:
        score += 2
    elif file_count > 5:
        score += 1

    score += blast_radius.get("sensitive_file_count", 0) * 3

    if blast_radius.get("has_infra_changes"):
        score += 3
    if blast_radius.get("has_dependency_changes"):
        score += 2

    module_count = len(blast_radius.get("modules_affected", []))
    if module_count > 3:
        score += 2
    elif module_count > 1:
        score += 1

    # Content findings scoring
    score += len(content_findings) * 2

    # Map score to risk level
    risk = "low"
    for level in ["critical", "high", "medium"]:
        if score >= RISK_THRESHOLDS[level]:
            risk = level
            break

    return risk, score


def _severity_meets_threshold(risk_level: str, threshold: str) -> bool:
    """Check if a risk level meets or exceeds a severity threshold."""
    risk_idx = SEVERITY_ORDER.index(risk_level) if risk_level in SEVERITY_ORDER else 0
    thresh_idx = SEVERITY_ORDER.index(threshold) if threshold in SEVERITY_ORDER else 2
    return risk_idx >= thresh_idx


def _write_journal_entry(
    instance_dir: str,
    project_name: str,
    risk_level: str,
    score: int,
    blast_radius: dict,
    content_findings: List[Tuple[str, str, str]],
    blocked: bool,
) -> None:
    """Write security review results to the project journal."""
    try:
        from app.utils import write_to_journal

        lines = [f"## Security Review — risk: {risk_level} (score: {score})"]

        br = blast_radius
        lines.append(
            f"- Files: {br['file_count']}, "
            f"Sensitive: {br['sensitive_file_count']}, "
            f"Modules: {len(br.get('modules_affected', []))}"
        )

        if br.get("has_infra_changes"):
            lines.append("- ⚠ Infrastructure changes detected")
        if br.get("has_dependency_changes"):
            lines.append("- ⚠ Dependency changes detected")

        if content_findings:
            lines.append(f"- Content findings ({len(content_findings)}):")
            # Show up to 10 findings to avoid journal bloat
            for desc, _match, context in content_findings[:10]:
                lines.append(f"  - {desc}: `{context[:80]}`")
            if len(content_findings) > 10:
                lines.append(f"  - ... and {len(content_findings) - 10} more")

        if blocked:
            lines.append("- **Auto-merge blocked** by security review")

        entry = "\n".join(lines)
        write_to_journal(instance_dir, entry)
    except Exception as e:
        print(f"[security_review] Journal write failed: {e}", file=sys.stderr)


def check_security_review(
    instance_dir: str,
    project_name: str,
    project_path: str,
) -> bool:
    """Run differential security review on the current branch.

    Analyzes the diff for security-sensitive patterns and blast radius.
    Configured via security_review section in projects.yaml.

    Args:
        instance_dir: Path to instance directory.
        project_name: Current project name.
        project_path: Path to project directory.

    Returns:
        True if auto-merge should proceed, False if blocked by review.
    """
    import os
    from app.projects_config import load_projects_config, get_project_security_review

    koan_root = os.environ.get("KOAN_ROOT", str(Path(instance_dir).parent))
    config = load_projects_config(koan_root)
    if not config:
        return True

    sr_config = get_project_security_review(config, project_name)
    if not sr_config.get("enabled"):
        return True

    # Get the base branch for diff comparison
    from app.projects_config import get_project_auto_merge
    merge_config = get_project_auto_merge(config, project_name)
    base_branch = merge_config.get("base_branch", "main")

    # Gather data
    changed_files = get_changed_files(project_path, base_branch)
    if not changed_files:
        return True  # No changes, nothing to review

    diff_text = get_diff_against_base(project_path, base_branch)
    content_findings = scan_diff_for_patterns(diff_text) if diff_text else []
    blast_radius = calculate_blast_radius(changed_files)

    # Assess risk
    risk_level, score = assess_risk_level(blast_radius, content_findings)

    # Determine if this should block auto-merge
    threshold = sr_config.get("severity_threshold", "high")
    blocking = sr_config.get("blocking", False)
    should_block = blocking and _severity_meets_threshold(risk_level, threshold)

    # Log to journal
    _write_journal_entry(
        instance_dir, project_name,
        risk_level, score, blast_radius, content_findings,
        blocked=should_block,
    )

    if should_block:
        print(
            f"[security_review] Blocking auto-merge: "
            f"risk={risk_level} score={score} threshold={threshold}",
        )
        return False

    if risk_level in ("high", "critical"):
        print(
            f"[security_review] Warning: "
            f"risk={risk_level} score={score} (non-blocking)",
        )

    return True
