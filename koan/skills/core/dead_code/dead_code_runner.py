"""
Koan -- Dead code scanner runner.

Performs a read-only dead code analysis of a project codebase and saves
the report to the project's memory directory. Optionally queues
top findings as removal missions.

Pipeline:
1. Build a dead code scan prompt with project context
2. Run Claude Code CLI (read-only tools) to analyze the codebase
3. Parse Claude's structured report
4. Save report to memory
5. Queue suggested missions

CLI:
    python3 -m skills.core.dead_code.dead_code_runner \
        --project-path <path> --project-name <name> --instance-dir <dir>
"""

import re
from pathlib import Path
from typing import Optional, Tuple

from app.prompts import load_prompt_or_skill


def build_dead_code_prompt(
    project_name: str,
    skill_dir: Optional[Path] = None,
) -> str:
    """Build a prompt for Claude to scan for dead code."""
    return load_prompt_or_skill(
        skill_dir, "dead_code",
        PROJECT_NAME=project_name,
    )


def _run_claude_scan(prompt: str, project_path: str) -> str:
    """Run Claude CLI with read-only tools and return the output text.

    Args:
        prompt: The dead code scan prompt.
        project_path: Path to the project for codebase context.

    Returns:
        Claude's analysis text, or empty string on failure.
    """
    from app.cli_provider import run_command_streaming
    from app.config import get_skill_timeout

    return run_command_streaming(
        prompt, project_path,
        allowed_tools=["Read", "Glob", "Grep"],
        max_turns=25,
        timeout=get_skill_timeout(),
    )


def _extract_report_body(raw_output: str) -> str:
    """Extract structured report from Claude's raw output.

    Tries to find the dead code report structure. Falls back to
    the full output if no structure is detected.
    """
    # Look for the report header
    match = re.search(r'(Dead Code Report\b.*)', raw_output, re.DOTALL)
    if match:
        return match.group(1).strip()

    # Look for ## Summary section
    match = re.search(r'(## Summary\b.*)', raw_output, re.DOTALL)
    if match:
        return match.group(1).strip()

    # Fall back to full output
    return raw_output.strip()


def _extract_dead_code_score(report: str) -> Optional[int]:
    """Extract the dead code score from the report.

    Returns the score as an integer (1-10) or None if not found.
    """
    match = re.search(r'\*\*Dead Code Score\*\*:\s*(\d+)/10', report)
    if match:
        score = int(match.group(1))
        if 1 <= score <= 10:
            return score
    return None


def _extract_missions(report: str) -> list:
    """Extract suggested missions from the report.

    Returns a list of mission title strings.
    """
    missions = []
    # Look for the Suggested Missions section
    match = re.search(
        r'## Suggested Missions\s*\n(.*?)(?:\n##|\n---|\Z)',
        report, re.DOTALL,
    )
    if not match:
        return missions

    section = match.group(1)
    for line in section.strip().splitlines():
        # Match numbered items: "1. Mission title" or "1. Mission title — addresses..."
        m = re.match(r'\d+\.\s+(.+?)(?:\s*[—\-]+\s*addresses.*)?$', line.strip())
        if m:
            title = m.group(1).strip()
            if title:
                missions.append(title)

    return missions[:5]


def _save_report(
    instance_dir: Path,
    project_name: str,
    report: str,
    dead_code_score: Optional[int],
) -> Path:
    """Save the dead code report to the project's memory directory.

    Returns the path to the saved report file.
    """
    from datetime import datetime as _dt

    memory_dir = instance_dir / "memory" / "projects" / project_name
    memory_dir.mkdir(parents=True, exist_ok=True)

    report_path = memory_dir / "dead_code.md"

    timestamp = _dt.now().strftime("%Y-%m-%d %H:%M")
    header = f"<!-- Last scan: {timestamp} -->\n"
    if dead_code_score is not None:
        header += f"<!-- Dead code score: {dead_code_score}/10 -->\n"
    header += "\n"

    report_path.write_text(header + report)
    return report_path


def _queue_missions(
    instance_dir: Path,
    project_name: str,
    missions: list,
    max_missions: int = 3,
) -> int:
    """Queue top suggested missions to missions.md.

    Returns the number of missions queued.
    """
    from app.utils import insert_pending_mission

    missions_path = instance_dir / "missions.md"
    queued = 0

    for title in missions[:max_missions]:
        entry = f"- [project:{project_name}] {title}"
        insert_pending_mission(missions_path, entry)
        queued += 1

    return queued


def run_dead_code(
    project_path: str,
    project_name: str,
    instance_dir: str,
    notify_fn=None,
    skill_dir: Optional[Path] = None,
    queue_missions: bool = True,
) -> Tuple[bool, str]:
    """Execute a dead code scan on a project.

    Args:
        project_path: Local path to the project.
        project_name: Project name for labeling.
        instance_dir: Path to instance directory.
        notify_fn: Optional callback for progress notifications.
        skill_dir: Optional path to the dead_code skill directory for prompts.
        queue_missions: Whether to queue suggested missions (default True).

    Returns:
        (success, summary) tuple.
    """
    if notify_fn is None:
        from app.notify import send_telegram
        notify_fn = send_telegram

    instance_path = Path(instance_dir)

    # Step 1: Build prompt
    notify_fn(f"\U0001f50d Scanning for dead code in {project_name}...")
    prompt = build_dead_code_prompt(project_name, skill_dir=skill_dir)

    # Step 2: Run Claude scan (read-only)
    try:
        raw_output = _run_claude_scan(prompt, project_path)
    except RuntimeError as e:
        return False, f"Dead code scan failed: {e}"

    if not raw_output:
        return False, f"Dead code scan produced no output for {project_name}."

    # Step 3: Extract structured report
    report = _extract_report_body(raw_output)
    dead_code_score = _extract_dead_code_score(report)

    # Step 4: Save report
    report_path = _save_report(instance_path, project_name, report, dead_code_score)

    # Step 5: Queue missions (unless disabled)
    missions = _extract_missions(report)
    queued = 0
    if queue_missions and missions:
        queued = _queue_missions(instance_path, project_name, missions)

    # Build summary
    score_text = f" (score: {dead_code_score}/10)" if dead_code_score is not None else ""
    queue_text = f", {queued} missions queued" if queued else ""
    summary = (
        f"Dead code report saved to {report_path.name}{score_text}{queue_text}"
    )
    notify_fn(f"\u2705 {summary}")

    return True, summary


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv=None):
    """CLI entry point for dead_code_runner."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Scan a project for dead code."
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
        "--no-queue", action="store_true",
        help="Don't queue suggested missions",
    )
    cli_args = parser.parse_args(argv)

    skill_dir = Path(__file__).resolve().parent

    success, summary = run_dead_code(
        project_path=cli_args.project_path,
        project_name=cli_args.project_name,
        instance_dir=cli_args.instance_dir,
        skill_dir=skill_dir,
        queue_missions=not cli_args.no_queue,
    )
    print(summary)
    return 0 if success else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
