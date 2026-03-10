"""
Kōan -- Post-mission semantic verification.

Validates that a mission's output aligns with its stated intent.
Complements pr_quality.py (which catches generic issues like debug prints
and secrets) by checking mission-specific semantic gaps:

- Did the branch produce meaningful changes?
- Were tests added for implementation/fix missions?
- Was a PR created for code-changing missions?
- Do commit messages and changed files relate to the mission title?

Part of the RARV discipline (issue #543): the Verify phase that runs
after Claude returns, providing independent validation before quality
gates and auto-merge.
"""

import re
import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

from app.git_utils import run_git, run_git_strict


class CheckStatus(Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


@dataclass
class Check:
    name: str
    status: CheckStatus
    message: str


@dataclass
class VerifyResult:
    passed: bool
    checks: List[Check] = field(default_factory=list)
    summary: str = ""

    @property
    def warnings(self) -> List[Check]:
        return [c for c in self.checks if c.status == CheckStatus.WARN]

    @property
    def failures(self) -> List[Check]:
        return [c for c in self.checks if c.status == CheckStatus.FAIL]


# ---------------------------------------------------------------------------
# Mission type classification (lightweight, for verification decisions)
# ---------------------------------------------------------------------------

# Missions that should produce code changes
CODE_MISSION_KEYWORDS = {
    "implement", "fix", "add", "create", "build", "refactor",
    "extract", "migrate", "update", "replace", "remove", "delete",
    "port", "rewrite", "optimize", "improve",
}

# Missions that should produce tests
TEST_EXPECTED_KEYWORDS = {
    "implement", "fix", "add", "create", "build", "refactor",
    "extract", "migrate", "feature",
}

# Missions that are analysis-only (no code changes expected)
ANALYSIS_KEYWORDS = {
    "audit", "review", "analyze", "investigate", "research",
    "assess", "evaluate", "report", "check", "explore",
    "plan", "document", "study",
}


def _is_code_mission(title: str) -> bool:
    """Check if mission title implies code changes."""
    words = set(re.findall(r'\b\w+\b', title.lower()))
    return bool(words & CODE_MISSION_KEYWORDS) and not bool(words & ANALYSIS_KEYWORDS)


def _is_analysis_mission(title: str) -> bool:
    """Check if mission title implies analysis-only work."""
    words = set(re.findall(r'\b\w+\b', title.lower()))
    return bool(words & ANALYSIS_KEYWORDS)


def _expects_tests(title: str) -> bool:
    """Check if mission type typically requires test additions."""
    words = set(re.findall(r'\b\w+\b', title.lower()))
    return bool(words & TEST_EXPECTED_KEYWORDS)


# ---------------------------------------------------------------------------
# Individual verification checks
# ---------------------------------------------------------------------------

def _get_base_ref(project_path: str) -> Optional[str]:
    """Determine the base ref for diffing."""
    for ref in ("upstream/main", "origin/main", "upstream/master", "origin/master"):
        rc, _, _ = run_git("rev-parse", "--verify", ref, cwd=project_path)
        if rc == 0:
            return ref
    return None


def check_diff_coherence(project_path: str, branch_prefix: str) -> Check:
    """Verify the branch has meaningful changes (not empty or trivially small).

    An empty branch after a code mission is a strong signal of failure.
    """
    rc, branch, _ = run_git("rev-parse", "--abbrev-ref", "HEAD", cwd=project_path)
    if rc != 0 or not branch:
        return Check("diff_coherence", CheckStatus.SKIP, "Could not determine branch")

    if branch in ("main", "master"):
        return Check("diff_coherence", CheckStatus.SKIP, "On main branch")

    if not branch.startswith(branch_prefix):
        return Check("diff_coherence", CheckStatus.SKIP, f"Not on {branch_prefix}* branch")

    base_ref = _get_base_ref(project_path)
    if not base_ref:
        return Check("diff_coherence", CheckStatus.SKIP, "No base ref found")

    # Count changed files
    rc, diff_stat, _ = run_git(
        "diff", "--stat", f"{base_ref}...HEAD", cwd=project_path
    )
    if rc != 0:
        return Check("diff_coherence", CheckStatus.SKIP, "Could not get diff stat")

    if not diff_stat.strip():
        return Check(
            "diff_coherence", CheckStatus.FAIL,
            "Branch has no changes compared to base"
        )

    # Count files changed (last line of --stat is summary)
    lines = diff_stat.strip().splitlines()
    file_count = len(lines) - 1  # Exclude summary line
    if file_count < 0:
        file_count = 0

    if file_count == 0:
        return Check(
            "diff_coherence", CheckStatus.FAIL,
            "Branch has no file changes"
        )

    return Check(
        "diff_coherence", CheckStatus.PASS,
        f"{file_count} file(s) changed"
    )


def check_test_coverage(project_path: str, mission_title: str) -> Check:
    """Verify that test files were modified for missions that should have tests.

    Only checks if test files were touched in the diff — does not run tests.
    """
    if not _expects_tests(mission_title):
        return Check(
            "test_coverage", CheckStatus.SKIP,
            "Mission type does not typically require tests"
        )

    base_ref = _get_base_ref(project_path)
    if not base_ref:
        return Check("test_coverage", CheckStatus.SKIP, "No base ref found")

    rc, changed_files, _ = run_git(
        "diff", "--name-only", f"{base_ref}...HEAD", cwd=project_path
    )
    if rc != 0 or not changed_files.strip():
        return Check("test_coverage", CheckStatus.SKIP, "Could not get changed files")

    files = changed_files.strip().splitlines()
    test_files = [
        f for f in files
        if re.search(r'(?:^|/)tests?/', f)
        or f.endswith(("_test.py", ".test.js", ".test.ts", ".spec.js", ".spec.ts", ".test.tsx", ".spec.tsx"))
    ]

    if not test_files:
        return Check(
            "test_coverage", CheckStatus.WARN,
            "No test files modified — consider adding test coverage"
        )

    return Check(
        "test_coverage", CheckStatus.PASS,
        f"{len(test_files)} test file(s) modified"
    )


def check_pr_created(project_path: str, mission_title: str) -> Check:
    """Verify that a draft PR was created for code-changing missions.

    Uses `gh pr view` to check for an existing PR on the current branch.
    """
    if _is_analysis_mission(mission_title):
        return Check(
            "pr_created", CheckStatus.SKIP,
            "Analysis mission — PR not required"
        )

    # Check if we're on a feature branch
    rc, branch, _ = run_git("rev-parse", "--abbrev-ref", "HEAD", cwd=project_path)
    if rc != 0 or branch in ("main", "master", ""):
        return Check("pr_created", CheckStatus.SKIP, "Not on feature branch")

    # Check for PR via gh CLI
    try:
        from app.github import run_gh
        pr_json = run_gh(
            "pr", "view", "--json", "number,state,isDraft",
            cwd=project_path, timeout=10,
        )
        import json
        pr_data = json.loads(pr_json)
        pr_num = pr_data.get("number")
        is_draft = pr_data.get("isDraft", False)
        state = pr_data.get("state", "")

        if state == "OPEN":
            draft_info = " (draft)" if is_draft else ""
            return Check(
                "pr_created", CheckStatus.PASS,
                f"PR #{pr_num}{draft_info} exists"
            )
        return Check(
            "pr_created", CheckStatus.WARN,
            f"PR #{pr_num} exists but state is {state}"
        )
    except Exception as e:
        # No PR or gh not available
        print(f"[verifier] PR check failed: {e}", file=sys.stderr)
        return Check(
            "pr_created", CheckStatus.WARN,
            "No PR found for current branch"
        )


def check_commit_quality(project_path: str) -> Check:
    """Verify commit messages are clean and well-formed.

    Checks for:
    - Empty commit messages
    - Leftover fixup/squash commits
    - Very short messages (< 10 chars)
    """
    base_ref = _get_base_ref(project_path)
    if not base_ref:
        return Check("commit_quality", CheckStatus.SKIP, "No base ref found")

    rc, log_output, _ = run_git(
        "log", "--format=%s", f"{base_ref}..HEAD", cwd=project_path
    )
    if rc != 0 or not log_output.strip():
        return Check("commit_quality", CheckStatus.SKIP, "No commits to check")

    messages = log_output.strip().splitlines()
    issues = []

    for msg in messages:
        if not msg.strip():
            issues.append("Empty commit message found")
        elif msg.startswith(("fixup!", "squash!", "amend!")):
            issues.append(f"Unsquashed commit: {msg[:60]}")
        elif len(msg.strip()) < 10:
            issues.append(f"Very short commit message: '{msg.strip()}'")

    if issues:
        return Check(
            "commit_quality", CheckStatus.WARN,
            "; ".join(issues[:3])
        )

    return Check(
        "commit_quality", CheckStatus.PASS,
        f"{len(messages)} commit(s), all well-formed"
    )


def check_mission_alignment(
    project_path: str, mission_title: str
) -> Check:
    """Heuristic check that changed files/commits relate to the mission title.

    Extracts keywords from the mission title and checks if they appear
    in changed file paths or commit messages. Low-confidence check —
    produces warnings, never failures.
    """
    if not mission_title.strip():
        return Check(
            "mission_alignment", CheckStatus.SKIP,
            "No mission title provided (autonomous session)"
        )

    # Extract meaningful keywords from title (3+ chars, not common words)
    # Check this BEFORE calling _get_base_ref to avoid unnecessary git calls
    stop_words = {
        "the", "and", "for", "from", "with", "that", "this", "add", "fix",
        "update", "create", "implement", "remove", "make", "use", "new",
        "get", "set", "run", "all", "not", "into", "via", "per", "has",
        "are", "was", "been", "will", "can", "should", "issue", "feat",
        "project", "koan",
    }
    title_words = set(re.findall(r'\b[a-z]\w{2,}\b', mission_title.lower()))
    keywords = title_words - stop_words

    if not keywords:
        return Check(
            "mission_alignment", CheckStatus.SKIP,
            "No meaningful keywords in mission title"
        )

    base_ref = _get_base_ref(project_path)
    if not base_ref:
        return Check("mission_alignment", CheckStatus.SKIP, "No base ref found")

    # Get changed files and commit messages
    rc, changed_files, _ = run_git(
        "diff", "--name-only", f"{base_ref}...HEAD", cwd=project_path
    )
    rc2, commits, _ = run_git(
        "log", "--format=%s", f"{base_ref}..HEAD", cwd=project_path
    )

    corpus = (changed_files + " " + commits).lower()

    # Check keyword overlap
    matched = {kw for kw in keywords if kw in corpus}
    ratio = len(matched) / len(keywords) if keywords else 0

    if ratio == 0:
        return Check(
            "mission_alignment", CheckStatus.WARN,
            f"No mission keywords found in changes (looked for: {', '.join(sorted(keywords)[:5])})"
        )
    elif ratio < 0.3:
        return Check(
            "mission_alignment", CheckStatus.WARN,
            f"Low keyword overlap ({len(matched)}/{len(keywords)}): {', '.join(sorted(matched))}"
        )

    return Check(
        "mission_alignment", CheckStatus.PASS,
        f"Keywords matched: {', '.join(sorted(matched))}"
    )


# ---------------------------------------------------------------------------
# Pipeline orchestrator
# ---------------------------------------------------------------------------

def verify_mission(
    project_path: str,
    mission_title: str = "",
    exit_code: int = 0,
    branch_prefix: str = "koan/",
) -> VerifyResult:
    """Run the full post-mission verification pipeline.

    Args:
        project_path: Path to the project directory.
        mission_title: Mission description (empty for autonomous sessions).
        exit_code: Claude CLI exit code.
        branch_prefix: Expected branch prefix.

    Returns:
        VerifyResult with all check outcomes.
    """
    checks: List[Check] = []

    # On failure, only run basic checks
    if exit_code != 0:
        checks.append(Check(
            "exit_code", CheckStatus.FAIL,
            f"Claude CLI exited with code {exit_code}"
        ))
        # Still check diff coherence to see if partial work was done
        try:
            checks.append(check_diff_coherence(project_path, branch_prefix))
        except Exception as e:
            print(f"[verifier] diff_coherence failed: {e}", file=sys.stderr)
        result = VerifyResult(
            passed=False,
            checks=checks,
            summary=f"Mission failed (exit code {exit_code})",
        )
        return result

    # On success, run all checks
    checks.append(Check("exit_code", CheckStatus.PASS, "Claude CLI exited successfully"))

    check_fns = [
        lambda: check_diff_coherence(project_path, branch_prefix),
        lambda: check_test_coverage(project_path, mission_title),
        lambda: check_pr_created(project_path, mission_title),
        lambda: check_commit_quality(project_path),
        lambda: check_mission_alignment(project_path, mission_title),
    ]

    for fn in check_fns:
        try:
            checks.append(fn())
        except Exception as e:
            name = fn.__name__ if hasattr(fn, '__name__') else "unknown"
            print(f"[verifier] {name} failed: {e}", file=sys.stderr)

    # Determine overall result
    failures = [c for c in checks if c.status == CheckStatus.FAIL]
    warnings = [c for c in checks if c.status == CheckStatus.WARN]

    passed = len(failures) == 0

    # Build summary
    parts = []
    if failures:
        parts.append(f"{len(failures)} failure(s)")
    if warnings:
        parts.append(f"{len(warnings)} warning(s)")
    passes = [c for c in checks if c.status == CheckStatus.PASS]
    if passes:
        parts.append(f"{len(passes)} passed")

    summary = ", ".join(parts) if parts else "All checks skipped"

    return VerifyResult(passed=passed, checks=checks, summary=summary)


def format_verify_result(result: VerifyResult) -> str:
    """Format verification result for logging/PR enrichment.

    Returns a compact multi-line string suitable for console output or
    PR comment insertion.
    """
    lines = [f"Verification: {'PASS' if result.passed else 'FAIL'} — {result.summary}"]

    for check in result.checks:
        if check.status == CheckStatus.SKIP:
            continue
        icon = {
            CheckStatus.PASS: "✓",
            CheckStatus.WARN: "⚠",
            CheckStatus.FAIL: "✗",
        }.get(check.status, "?")
        lines.append(f"  {icon} {check.name}: {check.message}")

    return "\n".join(lines)
