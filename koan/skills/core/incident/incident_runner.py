"""
Koan -- Incident runner.

Triages a production error: parses the error, identifies root cause,
checks recent commits for regressions, proposes a fix with tests,
and submits a draft PR.

CLI:
    python3 -m skills.core.incident.incident_runner --project-path <path> --error-file <file>
    python3 -m skills.core.incident.incident_runner --project-path <path> --error-text "TypeError: ..."
"""

import logging
import re
import time
from pathlib import Path
from typing import Optional, Tuple

from app.prompts import load_prompt_or_skill
from app.pr_submit import (
    get_current_branch,
    get_commit_subjects,
    guess_project_name,
    submit_draft_pr,
)

logger = logging.getLogger(__name__)

# Maximum error text length to keep prompt manageable.
_MAX_ERROR_LENGTH = 4000

# Pattern to extract structured summary from Claude output.
_SUMMARY_RE = re.compile(
    r"INCIDENT_SUMMARY_START\s*\n"
    r"root_cause:\s*(?P<root_cause>.+)\n"
    r"culprit_commit:\s*(?P<culprit_commit>.+)\n"
    r"fix_description:\s*(?P<fix_description>.+)\n"
    r"affected_files:\s*(?P<affected_files>.+)\n"
    r"pr_url:\s*(?P<pr_url>.+)\n"
    r"INCIDENT_SUMMARY_END",
)


def run_incident(
    project_path: str,
    error_text: str,
    instance_dir: Optional[str] = None,
    notify_fn=None,
    skill_dir: Optional[Path] = None,
) -> Tuple[bool, str]:
    """Execute the incident triage pipeline.

    Args:
        project_path: Local path to the project repository.
        error_text: The production error text (stack trace, log, etc.).
        instance_dir: Path to instance directory (for journaling).
        notify_fn: Notification function (defaults to send_telegram).
        skill_dir: Path to the incident skill directory for prompt loading.

    Returns:
        (success, summary) tuple.
    """
    if notify_fn is None:
        from app.notify import send_telegram
        notify_fn = send_telegram

    # Truncate very long error text
    if len(error_text) > _MAX_ERROR_LENGTH:
        error_text = error_text[:_MAX_ERROR_LENGTH] + "\n[... truncated]"

    error_preview = error_text[:80].replace("\n", " ")
    notify_fn(f"\U0001f6a8 Triaging incident: {error_preview}...")

    # Invoke Claude with the incident analysis prompt
    try:
        output = _execute_incident(
            project_path=project_path,
            error_text=error_text,
            skill_dir=skill_dir,
        )
    except Exception as e:
        summary = f"Incident triage failed: {str(e)[:300]}"
        _write_journal_entry(
            instance_dir, project_path, error_text,
            root_cause="Triage failed", fix_description=str(e)[:200],
            success=False,
        )
        return False, summary

    if not output:
        _write_journal_entry(
            instance_dir, project_path, error_text,
            root_cause="Claude returned empty output",
            fix_description="No analysis produced",
            success=False,
        )
        return False, "Claude returned empty output."

    # Parse structured summary from Claude output
    parsed = _parse_summary(output)

    # Post-incident: submit draft PR (Claude may have already created one)
    pr_url = parsed.get("pr_url") if parsed else None
    if not pr_url or pr_url == "none":
        pr_url = _submit_incident_pr(
            project_path=project_path,
            error_preview=error_preview,
            error_text=error_text,
            parsed=parsed,
        )

    # Journal the incident
    _write_journal_entry(
        instance_dir, project_path, error_text,
        root_cause=parsed.get("root_cause", "Unknown") if parsed else "Unknown",
        fix_description=parsed.get("fix_description", "") if parsed else "",
        culprit_commit=parsed.get("culprit_commit", "none") if parsed else "none",
        affected_files=parsed.get("affected_files", "") if parsed else "",
        pr_url=pr_url,
        success=True,
    )

    # Build notification and summary
    branch = get_current_branch(project_path)
    if pr_url and pr_url != "none":
        notify_fn(
            f"\u2705 Incident triaged and fix submitted\n"
            f"Draft PR: {pr_url}"
        )
        summary = f"Incident triaged. Draft PR: {pr_url}"
    elif branch not in ("main", "master"):
        notify_fn(
            f"\u2705 Incident triaged\n"
            f"Branch: {branch} (PR creation failed)"
        )
        summary = f"Incident triaged. Branch: {branch}"
    else:
        root = parsed.get("root_cause", "See analysis") if parsed else "See analysis"
        notify_fn(
            f"\u26a0\ufe0f Incident analyzed but no fix committed\n"
            f"Root cause: {root}"
        )
        summary = f"Incident analyzed. Root cause: {root}"

    return True, summary


def _execute_incident(
    project_path: str,
    error_text: str,
    skill_dir: Optional[Path] = None,
) -> str:
    """Execute the incident triage via Claude CLI."""
    from app.config import get_branch_prefix, get_skill_max_turns, get_skill_timeout

    branch_prefix = get_branch_prefix()
    timestamp = str(int(time.time()))

    prompt = _build_prompt(
        error_text=error_text,
        skill_dir=skill_dir,
        branch_prefix=branch_prefix,
        timestamp=timestamp,
    )

    from app.cli_provider import CLAUDE_TOOLS, run_command_streaming
    return run_command_streaming(
        prompt, project_path,
        allowed_tools=sorted(CLAUDE_TOOLS),
        max_turns=get_skill_max_turns(), timeout=get_skill_timeout(),
    )


def _build_prompt(
    error_text: str,
    skill_dir: Optional[Path] = None,
    branch_prefix: str = "koan/",
    timestamp: str = "",
) -> str:
    """Build the incident analysis prompt."""
    return load_prompt_or_skill(
        skill_dir, "incident-analyze",
        ERROR_TEXT=error_text,
        BRANCH_PREFIX=branch_prefix,
        TIMESTAMP=timestamp,
    )


def _parse_summary(output: str) -> Optional[dict]:
    """Extract structured incident summary from Claude output."""
    match = _SUMMARY_RE.search(output)
    if not match:
        return None
    return {
        "root_cause": match.group("root_cause").strip(),
        "culprit_commit": match.group("culprit_commit").strip(),
        "fix_description": match.group("fix_description").strip(),
        "affected_files": match.group("affected_files").strip(),
        "pr_url": match.group("pr_url").strip(),
    }


def _submit_incident_pr(
    project_path: str,
    error_preview: str,
    error_text: str,
    parsed: Optional[dict],
) -> Optional[str]:
    """Build incident-specific PR title/body and delegate to shared submit."""
    from app.projects_config import resolve_base_branch

    branch = get_current_branch(project_path)
    if branch in ("main", "master"):
        return None

    project_name = guess_project_name(project_path)
    base_branch = resolve_base_branch(project_name, project_path)
    commits = get_commit_subjects(project_path, base_branch=base_branch)

    if not commits:
        return None

    commits_text = "\n".join(f"- {s}" for s in commits)
    root_cause = parsed.get("root_cause", "See analysis") if parsed else "See analysis"

    pr_title = f"fix: {error_preview}"[:70]
    error_snippet = error_text[:500]
    pr_body = (
        f"## Summary\n\n"
        f"{root_cause}\n\n"
        f"## Incident Details\n\n"
        f"```\n{error_snippet}\n```\n\n"
        f"## Changes\n\n{commits_text}\n\n"
        f"---\n*Generated by Koan /incident*"
    )

    try:
        return submit_draft_pr(
            project_path=project_path,
            project_name=project_name,
            owner="",
            repo="",
            issue_number="",
            pr_title=pr_title,
            pr_body=pr_body,
        )
    except Exception as e:
        logger.warning("PR submission failed: %s", e)
        return None


def _write_journal_entry(
    instance_dir: Optional[str],
    project_path: str,
    error_text: str,
    root_cause: str = "Unknown",
    fix_description: str = "",
    culprit_commit: str = "none",
    affected_files: str = "",
    pr_url: Optional[str] = None,
    success: bool = True,
):
    """Write an incident entry to the daily journal."""
    if not instance_dir:
        return

    try:
        from app.journal import append_to_journal

        project_name = guess_project_name(project_path)
        error_preview = error_text[:200].replace("\n", " ")
        status = "Resolved" if success else "Escalated"

        parts = [
            f"\n### \U0001f6a8 Incident: {error_preview[:80]}\n",
            f"- **Status**: {status}\n",
            f"- **Root cause**: {root_cause}\n",
        ]
        if fix_description:
            parts.append(f"- **Fix**: {fix_description}\n")
        if culprit_commit and culprit_commit != "none":
            parts.append(f"- **Culprit commit**: {culprit_commit}\n")
        if affected_files:
            parts.append(f"- **Affected files**: {affected_files}\n")
        if pr_url and pr_url != "none":
            parts.append(f"- **PR**: {pr_url}\n")
        parts.append("")

        content = "".join(parts)
        append_to_journal(Path(instance_dir), project_name, content)
    except Exception as e:
        logger.warning("Failed to write incident journal entry: %s", e)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv=None):
    """CLI entry point for incident_runner."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Triage a production incident."
    )
    parser.add_argument(
        "--project-path", required=True,
        help="Local path to the project repository",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--error-file",
        help="Path to a file containing the error text",
    )
    group.add_argument(
        "--error-text",
        help="Error text (for short errors without shell escaping issues)",
    )
    parser.add_argument(
        "--instance-dir",
        help="Path to instance directory (for journaling)",
        default=None,
    )
    cli_args = parser.parse_args(argv)

    # Read error text from file or argument
    if cli_args.error_file:
        error_path = Path(cli_args.error_file)
        if not error_path.exists():
            print(f"Error file not found: {cli_args.error_file}")
            return 1
        error_text = error_path.read_text(encoding="utf-8")
    else:
        error_text = cli_args.error_text

    if not error_text.strip():
        print("Error text is empty.")
        return 1

    skill_dir = Path(__file__).resolve().parent

    success, summary = run_incident(
        project_path=cli_args.project_path,
        error_text=error_text,
        instance_dir=cli_args.instance_dir,
        skill_dir=skill_dir,
    )
    print(summary)
    return 0 if success else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
