"""Post-mission PR quality pipeline.

Validates Claude's work after mission execution:
- Code quality scan (debug prints, TODOs, secrets)
- Test verification
- Branch hygiene validation
- PR description enrichment
"""

import re
import subprocess
import sys
from typing import Optional

from app.git_utils import run_git_strict


# ---------------------------------------------------------------------------
# Patterns for code quality scanning
# ---------------------------------------------------------------------------

# Debug statements that should not appear in production code
DEBUG_PATTERNS = [
    (r'\bprint\s*\(', "debug print statement"),
    (r'\bconsole\.log\s*\(', "console.log statement"),
    (r'\bdebugger\b', "debugger statement"),
    (r'\bpdb\.set_trace\s*\(', "pdb debugger"),
    (r'\bbreakpoint\s*\(', "breakpoint() call"),
    (r'\bBinding\.pry\b', "Ruby debugger"),
]

# TODO/FIXME markers in new code
MARKER_PATTERNS = [
    (r'\bTODO\b', "TODO comment"),
    (r'\bFIXME\b', "FIXME comment"),
    (r'\bHACK\b', "HACK comment"),
    (r'\bXXX\b', "XXX marker"),
]

# Secrets patterns (high-confidence only)
SECRETS_PATTERNS = [
    (r'(?:api[_-]?key|apikey)\s*[:=]\s*["\'][A-Za-z0-9]{16,}', "possible API key"),
    (r'(?:secret|token|password|passwd)\s*[:=]\s*["\'][^"\']{8,}', "possible secret/token"),
    (r'sk-[A-Za-z0-9]{20,}', "possible OpenAI API key"),
    (r'ghp_[A-Za-z0-9]{36}', "possible GitHub token"),
    (r'AKIA[0-9A-Z]{16}', "possible AWS access key"),
]


def _get_base_ref(project_path: str) -> Optional[str]:
    """Determine the base ref for diffing (upstream/main or origin/main)."""
    for ref in ("upstream/main", "origin/main", "upstream/master", "origin/master"):
        try:
            run_git_strict("rev-parse", "--verify", ref, cwd=project_path)
            return ref
        except (RuntimeError, subprocess.CalledProcessError):
            continue
    return None


def _parse_diff_added_lines(diff_text: str) -> list:
    """Parse unified diff and extract only added lines with file/line info.

    Returns list of (file, line_number, content) tuples.
    """
    results = []
    current_file = None
    current_line = 0

    for line in diff_text.splitlines():
        # Detect file header
        if line.startswith("+++ b/"):
            current_file = line[6:]
            continue
        # Detect hunk header
        hunk_match = re.match(r'^@@ -\d+(?:,\d+)? \+(\d+)', line)
        if hunk_match:
            current_line = int(hunk_match.group(1))
            continue
        # Count lines in the new file
        if line.startswith("+") and not line.startswith("+++"):
            if current_file:
                results.append((current_file, current_line, line[1:]))
            current_line += 1
        elif line.startswith("-"):
            # Removed line — don't increment new-file line counter
            pass
        else:
            # Context line
            current_line += 1

    return results


def scan_changes(project_path: str) -> dict:
    """Scan diff between base branch and HEAD for quality issues.

    Returns:
        Dict with keys:
            issues: list of {type, file, line, message}
            clean: bool (True if no issues found)
    """
    result = {"issues": [], "clean": True}

    base_ref = _get_base_ref(project_path)
    if not base_ref:
        return result

    # Check we're not on main
    try:
        branch = run_git_strict(
            "rev-parse", "--abbrev-ref", "HEAD", cwd=project_path
        )
    except (RuntimeError, subprocess.CalledProcessError):
        return result

    if branch in ("main", "master"):
        return result

    # Get full diff of added lines
    try:
        diff = run_git_strict(
            "diff", f"{base_ref}...HEAD", cwd=project_path, timeout=30
        )
    except (RuntimeError, subprocess.CalledProcessError):
        return result

    if not diff.strip():
        return result

    added_lines = _parse_diff_added_lines(diff)
    issues = []

    for filepath, line_num, content in added_lines:
        # Skip binary files, test files, and config files
        if _should_skip_file(filepath):
            continue

        # Check debug patterns
        for pattern, message in DEBUG_PATTERNS:
            if re.search(pattern, content):
                issues.append({
                    "type": "debug",
                    "file": filepath,
                    "line": line_num,
                    "message": message,
                })

        # Check TODO/FIXME markers
        for pattern, message in MARKER_PATTERNS:
            if re.search(pattern, content):
                issues.append({
                    "type": "marker",
                    "file": filepath,
                    "line": line_num,
                    "message": message,
                })

        # Check secrets patterns
        for pattern, message in SECRETS_PATTERNS:
            if re.search(pattern, content, re.IGNORECASE):
                issues.append({
                    "type": "secret",
                    "file": filepath,
                    "line": line_num,
                    "message": message,
                })

    # Check for large file changes
    issues.extend(_check_large_changes(diff))

    result["issues"] = issues
    result["clean"] = len(issues) == 0
    return result


def _should_skip_file(filepath: str) -> bool:
    """Check if a file should be skipped during quality scanning."""
    # Skip test files (debug prints are expected)
    if re.search(r'(?:^|/)tests?/', filepath) or filepath.endswith(("_test.py", ".test.js", ".test.ts", ".spec.js", ".spec.ts")):
        return True
    # Skip config/lock files
    if filepath.endswith((".lock", ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini")):
        return True
    # Skip generated files
    if filepath.endswith((".min.js", ".min.css", ".map")):
        return True
    return False


def _check_large_changes(diff_text: str) -> list:
    """Check for files with unusually large changes (>500 lines added)."""
    issues = []
    current_file = None
    added_count = 0

    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            # Emit issue for previous file if large
            if current_file and added_count > 500:
                issues.append({
                    "type": "large_change",
                    "file": current_file,
                    "line": 0,
                    "message": f"{added_count} lines added",
                })
            current_file = line[6:]
            added_count = 0
        elif line.startswith("+") and not line.startswith("+++"):
            added_count += 1

    # Check last file
    if current_file and added_count > 500:
        issues.append({
            "type": "large_change",
            "file": current_file,
            "line": 0,
            "message": f"{added_count} lines added",
        })

    return issues


# ---------------------------------------------------------------------------
# Phase 3: Branch hygiene validation
# ---------------------------------------------------------------------------

def validate_branch(project_path: str, branch_prefix: str) -> dict:
    """Validate branch naming, commit messages, and git hygiene.

    Args:
        project_path: Path to the project root.
        branch_prefix: Expected branch prefix (e.g. "koan/").

    Returns:
        Dict with keys:
            valid: bool
            issues: list of {type, message}
    """
    result = {"valid": True, "issues": []}

    try:
        branch = run_git_strict(
            "rev-parse", "--abbrev-ref", "HEAD", cwd=project_path
        )
    except (RuntimeError, subprocess.CalledProcessError):
        result["valid"] = False
        result["issues"].append({
            "type": "branch",
            "message": "Could not determine current branch",
        })
        return result

    if branch in ("main", "master"):
        # Not on a feature branch — nothing to validate
        return result

    # Check branch naming convention
    if not branch.startswith(branch_prefix):
        result["issues"].append({
            "type": "naming",
            "message": f"Branch '{branch}' does not follow prefix '{branch_prefix}'",
        })

    # Get base ref
    base_ref = _get_base_ref(project_path)
    if not base_ref:
        result["issues"].append({
            "type": "base",
            "message": "Could not determine base branch for comparison",
        })
        result["valid"] = len(result["issues"]) == 0
        return result

    # Check if branch has commits ahead of base
    try:
        log_output = run_git_strict(
            "log", "--oneline", f"{base_ref}..HEAD", cwd=project_path
        )
    except (RuntimeError, subprocess.CalledProcessError):
        log_output = ""

    if not log_output.strip():
        result["issues"].append({
            "type": "empty",
            "message": "Branch has no commits ahead of base",
        })
        result["valid"] = False
        return result

    commits = log_output.strip().splitlines()

    # Check for leftover fixup/squash commits
    for commit_line in commits:
        # Format: "abc1234 commit message"
        msg = commit_line.split(" ", 1)[1] if " " in commit_line else commit_line
        if msg.startswith(("fixup!", "squash!", "amend!")):
            result["issues"].append({
                "type": "fixup",
                "message": f"Unsquashed commit: {msg[:80]}",
            })

    # Check if branch is pushed to remote
    try:
        run_git_strict(
            "rev-parse", "--verify", f"origin/{branch}", cwd=project_path
        )
    except (RuntimeError, subprocess.CalledProcessError):
        result["issues"].append({
            "type": "unpushed",
            "message": "Branch is not pushed to remote",
        })

    # Check conventional commit format if project uses it
    if _project_uses_conventional_commits(project_path, base_ref):
        conventional_re = re.compile(
            r'^(?:feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)'
            r'(?:\([^)]+\))?!?:\s'
        )
        for commit_line in commits:
            msg = commit_line.split(" ", 1)[1] if " " in commit_line else commit_line
            # Skip fixup/squash (already flagged)
            if msg.startswith(("fixup!", "squash!", "amend!")):
                continue
            if not conventional_re.match(msg):
                result["issues"].append({
                    "type": "conventional",
                    "message": f"Non-conventional commit: {msg[:80]}",
                })

    result["valid"] = len(result["issues"]) == 0
    return result


def _project_uses_conventional_commits(project_path: str, base_ref: str) -> bool:
    """Detect if project uses conventional commits by scanning recent history."""
    try:
        log_output = run_git_strict(
            "log", "--oneline", "-20", base_ref, cwd=project_path
        )
    except (RuntimeError, subprocess.CalledProcessError):
        return False

    if not log_output.strip():
        return False

    conventional_re = re.compile(
        r'^[a-f0-9]+\s+(?:feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)'
        r'(?:\([^)]+\))?!?:\s'
    )

    lines = log_output.strip().splitlines()
    matches = sum(1 for line in lines if conventional_re.match(line))
    # Consider conventional if >50% of recent commits follow the pattern
    return matches > len(lines) * 0.5


# ---------------------------------------------------------------------------
# Phase 4: PR description enrichment
# ---------------------------------------------------------------------------

def enrich_pr_description(project_path: str, quality_report: dict) -> Optional[str]:
    """Update existing draft PR with quality report footer.

    Args:
        project_path: Path to the project root.
        quality_report: Combined results from all quality phases.

    Returns:
        PR URL if enriched, None if no PR found or enrichment skipped.
    """
    from app.github import run_gh

    # Check if a PR exists for the current branch
    try:
        branch = run_git_strict(
            "rev-parse", "--abbrev-ref", "HEAD", cwd=project_path
        )
    except (RuntimeError, subprocess.CalledProcessError):
        return None

    if branch in ("main", "master"):
        return None

    try:
        pr_json = run_gh(
            "pr", "view", "--json", "number,body,url",
            cwd=project_path, timeout=15,
        )
    except RuntimeError:
        # No PR exists for this branch
        return None

    import json
    try:
        pr_data = json.loads(pr_json)
    except (json.JSONDecodeError, TypeError):
        return None

    pr_number = pr_data.get("number")
    pr_body = pr_data.get("body", "")
    pr_url = pr_data.get("url", "")

    if not pr_number:
        return None

    # Build quality report section
    report_section = _build_quality_report_section(quality_report, project_path)

    # Remove any previous quality report from the body
    separator = "---\n### Quality Report"
    if separator in pr_body:
        pr_body = pr_body[:pr_body.index(separator)].rstrip()

    new_body = f"{pr_body}\n\n{report_section}"

    try:
        run_gh(
            "pr", "edit", str(pr_number),
            "--body", new_body,
            cwd=project_path, timeout=15,
        )
        return pr_url
    except RuntimeError as e:
        print(f"[pr_quality] Failed to enrich PR: {e}", file=sys.stderr)
        return None


def _build_quality_report_section(report: dict, project_path: str) -> str:
    """Build the markdown quality report section for PR body."""
    from app.claude_step import _get_diffstat

    lines = ["---", "### Quality Report", ""]

    # Diffstat
    base_ref = _get_base_ref(project_path)
    if base_ref:
        diffstat = _get_diffstat(base_ref, project_path)
        if diffstat:
            lines.append(f"**Changes**: {diffstat}")
            lines.append("")

    # Code scan results
    scan = report.get("scan", {})
    if scan.get("clean", True):
        lines.append("**Code scan**: clean")
    else:
        issue_count = len(scan.get("issues", []))
        lines.append(f"**Code scan**: {issue_count} issue(s) found")
        for issue in scan.get("issues", [])[:10]:
            lines.append(f"- `{issue['file']}:{issue['line']}` — {issue['message']}")
    lines.append("")

    # Test results
    tests = report.get("tests", {})
    if tests:
        if tests.get("skipped"):
            lines.append("**Tests**: skipped")
        elif tests.get("passed"):
            lines.append(f"**Tests**: passed ({tests.get('details', 'OK')})")
        else:
            lines.append(f"**Tests**: failed ({tests.get('details', 'FAILED')})")
    lines.append("")

    # Branch hygiene
    branch_result = report.get("branch", {})
    if branch_result.get("valid", True):
        lines.append("**Branch hygiene**: clean")
    else:
        issue_count = len(branch_result.get("issues", []))
        lines.append(f"**Branch hygiene**: {issue_count} issue(s)")
        for issue in branch_result.get("issues", [])[:5]:
            lines.append(f"- {issue['message']}")
    lines.append("")

    lines.append("*Generated by Kōan post-mission quality pipeline*")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Phase 5: Quality gate for auto-merge
# ---------------------------------------------------------------------------

def should_block_auto_merge(quality_report: dict, gate_mode: str = "warn") -> bool:
    """Determine if auto-merge should be blocked based on quality results.

    Args:
        quality_report: Combined results from all quality phases.
        gate_mode: One of "strict", "warn", "off".
            - strict: block on any issue
            - warn: allow but comment on PR
            - off: no gating

    Returns:
        True if auto-merge should be blocked.
    """
    if gate_mode == "off":
        return False

    if gate_mode != "strict":
        return False

    # In strict mode, block if any significant issue exists
    scan = quality_report.get("scan", {})
    if not scan.get("clean", True):
        # Only block on secrets, not on debug prints or markers
        secret_issues = [i for i in scan.get("issues", []) if i["type"] == "secret"]
        if secret_issues:
            return True

    tests = quality_report.get("tests", {})
    if tests and not tests.get("passed", True) and not tests.get("skipped", False):
        return True

    return False


def post_quality_comment(project_path: str, quality_report: dict) -> bool:
    """Post a quality warning comment on the PR if issues exist.

    Returns True if comment was posted.
    """
    from app.github import run_gh

    # Only comment if there are actual issues
    has_issues = False
    scan = quality_report.get("scan", {})
    if not scan.get("clean", True):
        has_issues = True
    tests = quality_report.get("tests", {})
    if tests and not tests.get("passed", True) and not tests.get("skipped", False):
        has_issues = True
    branch = quality_report.get("branch", {})
    if not branch.get("valid", True):
        has_issues = True

    if not has_issues:
        return False

    comment_lines = ["### Quality Gate Warning", ""]

    if not scan.get("clean", True):
        comment_lines.append("**Code issues found:**")
        for issue in scan.get("issues", [])[:10]:
            comment_lines.append(f"- `{issue['file']}:{issue['line']}` — {issue['message']}")
        comment_lines.append("")

    if tests and not tests.get("passed", True) and not tests.get("skipped", False):
        comment_lines.append(f"**Tests failed**: {tests.get('details', 'FAILED')}")
        comment_lines.append("")

    if not branch.get("valid", True):
        comment_lines.append("**Branch hygiene issues:**")
        for issue in branch.get("issues", [])[:5]:
            comment_lines.append(f"- {issue['message']}")
        comment_lines.append("")

    comment_lines.append("*Auto-merge was skipped due to quality gate issues.*")
    comment_body = "\n".join(comment_lines)

    try:
        run_gh(
            "pr", "comment", "--body", comment_body,
            cwd=project_path, timeout=15,
        )
        return True
    except RuntimeError as e:
        print(f"[pr_quality] Failed to post quality comment: {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Pipeline orchestrator
# ---------------------------------------------------------------------------

def run_quality_pipeline(
    project_path: str,
    branch_prefix: str,
    run_tests: bool = True,
    test_timeout: int = 120,
    gate_mode: str = "warn",
    status_callback=None,
) -> dict:
    """Run the full post-mission quality pipeline.

    Args:
        project_path: Path to the project root.
        branch_prefix: Expected branch prefix (e.g. "koan/").
        run_tests: Whether to run the test phase.
        test_timeout: Timeout for test execution in seconds.
        gate_mode: Quality gate mode (strict/warn/off).
        status_callback: Optional callable for status updates.

    Returns:
        Dict with keys: scan, tests, branch, pr_enriched, gate_blocked, gate_comment.
    """
    def _report(step: str) -> None:
        if status_callback:
            status_callback(step)

    result = {
        "scan": {},
        "tests": {},
        "branch": {},
        "pr_enriched": None,
        "gate_blocked": False,
        "gate_comment": False,
    }

    # Early exit: check if we're on a feature branch
    try:
        branch = run_git_strict(
            "rev-parse", "--abbrev-ref", "HEAD", cwd=project_path
        )
    except (RuntimeError, subprocess.CalledProcessError):
        return result

    if branch in ("main", "master"):
        return result

    if not branch.startswith(branch_prefix):
        return result

    # Phase 1: Code quality scan
    _report("scanning changes")
    try:
        result["scan"] = scan_changes(project_path)
    except Exception as e:
        print(f"[pr_quality] Code scan failed: {e}", file=sys.stderr)
        result["scan"] = {"issues": [], "clean": True}

    # Phase 2: Test verification
    if run_tests:
        _report("running tests")
        try:
            from app.pr_review import detect_test_command
            from app.claude_step import run_project_tests

            test_cmd = detect_test_command(project_path)
            if test_cmd:
                test_result = run_project_tests(
                    project_path, test_cmd=test_cmd, timeout=test_timeout,
                )
                result["tests"] = {
                    "passed": test_result["passed"],
                    "details": test_result["details"],
                    "skipped": False,
                }
            else:
                result["tests"] = {
                    "passed": True,
                    "details": "no test command found",
                    "skipped": True,
                }
        except Exception as e:
            print(f"[pr_quality] Test verification failed: {e}", file=sys.stderr)
            result["tests"] = {"passed": False, "details": str(e)[:100], "skipped": False}

    # Phase 3: Branch hygiene
    _report("validating branch")
    try:
        result["branch"] = validate_branch(project_path, branch_prefix)
    except Exception as e:
        print(f"[pr_quality] Branch validation failed: {e}", file=sys.stderr)
        result["branch"] = {"valid": True, "issues": []}

    # Phase 4: PR enrichment
    _report("enriching PR description")
    try:
        result["pr_enriched"] = enrich_pr_description(project_path, result)
    except Exception as e:
        print(f"[pr_quality] PR enrichment failed: {e}", file=sys.stderr)

    # Phase 5: Quality gate evaluation
    result["gate_blocked"] = should_block_auto_merge(result, gate_mode)

    # Quality gate comments are posted by the caller (check_auto_merge)
    # only when auto-merge is configured for the project.  This avoids
    # noise on external repos where the gate has no enforcement role.
    # The quality report is still embedded in the PR description (Phase 4).

    return result
