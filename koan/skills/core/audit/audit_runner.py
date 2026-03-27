"""
Koan -- Codebase audit runner.

Performs a read-only audit of a project codebase, parses the structured
findings, and creates individual GitHub issues for each one.

Pipeline:
1. Build audit prompt with project context and optional extra guidance
2. Run Claude Code CLI (read-only tools) to analyze the codebase
3. Parse Claude's structured findings (---FINDING--- blocks)
4. Enforce max_issues limit (keep only top N by severity)
5. Create a GitHub issue for each finding
6. Save audit summary to project learnings

CLI:
    python3 -m skills.core.audit.audit_runner \
        --project-path <path> --project-name <name> --instance-dir <dir> \
        [--context "focus on auth module"] [--max-issues 5]
"""

import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from app.prompts import load_prompt_or_skill

DEFAULT_MAX_ISSUES = 5

_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

class AuditFinding:
    """A single finding from the audit."""

    __slots__ = (
        "title", "severity", "category", "location",
        "problem", "why", "suggested_fix", "effort",
    )

    def __init__(
        self,
        title: str = "",
        severity: str = "medium",
        category: str = "",
        location: str = "",
        problem: str = "",
        why: str = "",
        suggested_fix: str = "",
        effort: str = "medium",
    ):
        self.title = title
        self.severity = severity
        self.category = category
        self.location = location
        self.problem = problem
        self.why = why
        self.suggested_fix = suggested_fix
        self.effort = effort

    def is_valid(self) -> bool:
        """Check if the finding has the minimum required fields."""
        return bool(self.title and self.problem and self.location)


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

def build_audit_prompt(
    project_name: str,
    extra_context: str = "",
    skill_dir: Optional[Path] = None,
    max_issues: int = DEFAULT_MAX_ISSUES,
) -> str:
    """Build the audit prompt with optional extra context and issue limit."""
    context_block = ""
    if extra_context:
        context_block = (
            f"## Additional Focus\n\n"
            f"The human has asked you to pay special attention to:\n"
            f"> {extra_context}\n\n"
            f"Prioritize findings related to this guidance, but don't "
            f"ignore other significant issues you discover."
        )

    return load_prompt_or_skill(
        skill_dir, "audit",
        PROJECT_NAME=project_name,
        EXTRA_CONTEXT=context_block,
        MAX_ISSUES=str(max_issues),
    )


# ---------------------------------------------------------------------------
# Claude CLI integration
# ---------------------------------------------------------------------------

def _run_claude_audit(prompt: str, project_path: str) -> str:
    """Run Claude CLI with read-only tools and return the output text."""
    from app.cli_provider import run_command_streaming
    from app.config import get_skill_timeout

    return run_command_streaming(
        prompt, project_path,
        allowed_tools=["Read", "Glob", "Grep", "Bash(git log:*)"],
        max_turns=30,
        timeout=get_skill_timeout(),
    )


# ---------------------------------------------------------------------------
# Finding parser
# ---------------------------------------------------------------------------

_FIELD_RE = re.compile(
    r"^(TITLE|SEVERITY|CATEGORY|LOCATION|PROBLEM|WHY|SUGGESTED_FIX|EFFORT):\s*(.+)",
    re.MULTILINE,
)


def parse_findings(raw_output: str) -> List[AuditFinding]:
    """Parse ---FINDING--- blocks from Claude's output."""
    blocks = re.split(r"---FINDING---", raw_output)

    findings = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue

        finding = AuditFinding()
        for match in _FIELD_RE.finditer(block):
            field = match.group(1).lower()
            value = match.group(2).strip()

            # For multiline fields, capture everything until the next field
            end_pos = match.end()
            next_field = _FIELD_RE.search(block[end_pos:])
            if next_field:
                full_value = block[match.start(2):end_pos + next_field.start()].strip()
            else:
                full_value = block[match.start(2):].strip()

            # Use the full multiline value for content fields
            if field in ("problem", "why", "suggested_fix"):
                value = full_value

            setattr(finding, field, value)

        if finding.is_valid():
            findings.append(finding)

    return findings


def prioritize_findings(
    findings: List[AuditFinding],
    max_issues: int = DEFAULT_MAX_ISSUES,
) -> List[AuditFinding]:
    """Keep only the top *max_issues* findings, ranked by severity.

    Severity order: critical > high > medium > low.
    Ties preserve the original order from the audit output.
    """
    if len(findings) <= max_issues:
        return findings

    # Stable sort by severity (critical first)
    ranked = sorted(
        findings,
        key=lambda f: _SEVERITY_ORDER.get(f.severity, 99),
    )
    return ranked[:max_issues]


# ---------------------------------------------------------------------------
# GitHub issue creation
# ---------------------------------------------------------------------------

_SEVERITY_LABELS = {
    "critical": "\U0001f534",  # red circle
    "high": "\U0001f7e0",      # orange circle
    "medium": "\U0001f7e1",    # yellow circle
    "low": "\U0001f7e2",       # green circle
}

_EFFORT_LABELS = {
    "small": "\u26a1 Quick fix",
    "medium": "\U0001f6e0\ufe0f Moderate effort",
    "large": "\U0001f3d7\ufe0f Significant work",
}


def _build_issue_body(finding: AuditFinding) -> str:
    """Build a GitHub issue body from a finding."""
    severity_icon = _SEVERITY_LABELS.get(finding.severity, "\u2753")
    effort_label = _EFFORT_LABELS.get(finding.effort, finding.effort)

    lines = [
        f"## Problem",
        f"",
        f"{finding.problem}",
        f"",
        f"## Why This Matters",
        f"",
        f"{finding.why}",
        f"",
        f"## Suggested Fix",
        f"",
        f"{finding.suggested_fix}",
        f"",
        f"## Details",
        f"",
        f"| | |",
        f"|---|---|",
        f"| **Severity** | {severity_icon} {finding.severity.capitalize()} |",
        f"| **Category** | {finding.category} |",
        f"| **Location** | `{finding.location}` |",
        f"| **Effort** | {effort_label} |",
        f"",
        f"---",
        f"\U0001f916 Created by K\u014dan from audit session",
    ]
    return "\n".join(lines)


def create_issues(
    findings: List[AuditFinding],
    project_path: str,
    notify_fn=None,
) -> List[str]:
    """Create GitHub issues for each finding.

    Returns a list of issue URLs.
    """
    from app.github import issue_create, resolve_target_repo

    target_repo = resolve_target_repo(project_path)
    issue_urls = []

    for i, finding in enumerate(findings, 1):
        title = finding.title
        body = _build_issue_body(finding)

        if notify_fn:
            notify_fn(
                f"  \U0001f4dd Creating issue {i}/{len(findings)}: {title}"
            )

        try:
            url = issue_create(
                title=title,
                body=body,
                repo=target_repo,
                cwd=project_path,
            )
            url = url.strip()
            issue_urls.append(url)
            if notify_fn and url:
                notify_fn(f"  \U0001f517 {url}")
        except Exception as e:
            print(
                f"[audit] Failed to create issue '{title}': {e}",
                file=sys.stderr,
            )

    return issue_urls


# ---------------------------------------------------------------------------
# Report saving
# ---------------------------------------------------------------------------

def _save_audit_report(
    instance_dir: Path,
    project_name: str,
    findings: List[AuditFinding],
    issue_urls: List[str],
) -> Path:
    """Save the audit summary to the project's learnings directory."""
    from datetime import datetime as _dt

    learnings_dir = instance_dir / "memory" / "projects" / project_name
    learnings_dir.mkdir(parents=True, exist_ok=True)

    report_path = learnings_dir / "audit.md"

    timestamp = _dt.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"<!-- Last audit: {timestamp} -->",
        f"<!-- Findings: {len(findings)} -->",
        f"",
        f"# Audit Report — {project_name}",
        f"",
    ]

    for i, finding in enumerate(findings):
        url = issue_urls[i] if i < len(issue_urls) else "no issue created"
        lines.append(
            f"- [{finding.severity}] {finding.title} "
            f"(`{finding.location}`) — {url}"
        )

    lines.append("")
    report_path.write_text("\n".join(lines))
    return report_path


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def run_audit(
    project_path: str,
    project_name: str,
    instance_dir: str,
    extra_context: str = "",
    max_issues: int = DEFAULT_MAX_ISSUES,
    notify_fn=None,
    skill_dir: Optional[Path] = None,
) -> Tuple[bool, str]:
    """Execute a codebase audit on a project.

    Args:
        project_path: Local path to the project.
        project_name: Project name for labeling.
        instance_dir: Path to instance directory.
        extra_context: Optional focus guidance from the user.
        max_issues: Maximum number of findings to create issues for.
        notify_fn: Optional callback for progress notifications.
        skill_dir: Optional path to the audit skill directory for prompts.

    Returns:
        (success, summary) tuple.
    """
    if notify_fn is None:
        from app.notify import send_telegram
        notify_fn = send_telegram

    instance_path = Path(instance_dir)

    # Step 1: Build prompt
    context_hint = f" (focus: {extra_context})" if extra_context else ""
    notify_fn(f"\U0001f50e Auditing {project_name}{context_hint}...")
    prompt = build_audit_prompt(
        project_name, extra_context, skill_dir=skill_dir,
        max_issues=max_issues,
    )

    # Step 2: Run Claude audit (read-only)
    try:
        raw_output = _run_claude_audit(prompt, project_path)
    except RuntimeError as e:
        return False, f"Audit failed: {e}"

    if not raw_output:
        return False, f"Audit produced no output for {project_name}."

    # Step 3: Parse findings
    findings = parse_findings(raw_output)
    if not findings:
        notify_fn(f"\u2705 Audit of {project_name} found no actionable issues.")
        return True, "Audit completed — no findings."

    # Step 4: Enforce max_issues limit (keep top N by severity)
    original_count = len(findings)
    findings = prioritize_findings(findings, max_issues)
    if len(findings) < original_count:
        notify_fn(
            f"\U0001f4cb Found {original_count} issue(s), "
            f"keeping top {len(findings)}. Creating GitHub issues..."
        )
    else:
        notify_fn(
            f"\U0001f4cb Found {len(findings)} issue(s). "
            f"Creating GitHub issues..."
        )

    # Step 5: Create GitHub issues
    issue_urls = create_issues(findings, project_path, notify_fn=notify_fn)

    # Step 6: Save report
    report_path = _save_audit_report(
        instance_path, project_name, findings, issue_urls,
    )

    # Build summary
    summary = (
        f"Audit complete: {len(findings)} findings, "
        f"{len(issue_urls)} GitHub issues created. "
        f"Report saved to {report_path.name}."
    )
    notify_fn(f"\u2705 {summary}")

    return True, summary


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv=None):
    """CLI entry point for audit_runner."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Audit a project codebase and create GitHub issues."
    )
    parser.add_argument(
        "--project-path", required=True,
        help="Local path to the project repository",
    )
    parser.add_argument(
        "--project-name", required=True,
        help="Project name for labeling",
    )
    parser.add_argument(
        "--instance-dir", required=True,
        help="Path to instance directory",
    )
    parser.add_argument(
        "--context", default="",
        help="Optional focus context for the audit",
    )
    parser.add_argument(
        "--context-file", default=None,
        help="Read context from a file (for long text)",
    )
    parser.add_argument(
        "--max-issues", type=int, default=DEFAULT_MAX_ISSUES,
        help=f"Maximum number of findings to create issues for (default: {DEFAULT_MAX_ISSUES})",
    )
    cli_args = parser.parse_args(argv)

    # Context from file takes precedence
    context = cli_args.context
    if cli_args.context_file:
        try:
            context = Path(cli_args.context_file).read_text(encoding="utf-8").strip()
        except OSError as e:
            print(f"Warning: could not read context file: {e}", file=sys.stderr)

    skill_dir = Path(__file__).resolve().parent

    success, summary = run_audit(
        project_path=cli_args.project_path,
        project_name=cli_args.project_name,
        instance_dir=cli_args.instance_dir,
        extra_context=context,
        max_issues=cli_args.max_issues,
        skill_dir=skill_dir,
    )
    print(summary)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
