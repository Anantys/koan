#!/usr/bin/env python3
"""
Kōan -- Daily rituals module

Morning brief (first run) and evening debrief (last run) — conversational
messages that make Kōan feel like a collaborator, not just a tool.

Usage: python -m app.rituals <morning|evening> <instance_dir>
"""

import os
import subprocess
import sys
from pathlib import Path

from app.cli_exec import run_cli
from app.cli_provider import build_full_command
from app.claude_step import strip_cli_noise
from app.prompts import get_prompt_path


def load_template(template_name: str, instance_dir: Path) -> str:
    """Load and prepare a ritual template.

    Args:
        template_name: "morning-brief" or "evening-debrief"
        instance_dir: Path to instance directory

    Returns:
        Template content with placeholders resolved
    """
    template_path = get_prompt_path(template_name)

    if not template_path.exists():
        raise FileNotFoundError(f"Template not found: {template_path}")

    content = template_path.read_text()
    content = content.replace("{INSTANCE}", str(instance_dir))
    return content


def run_ritual(ritual_type: str, instance_dir: Path) -> bool:
    """Execute a ritual via Claude CLI.

    Args:
        ritual_type: "morning" or "evening"
        instance_dir: Path to instance directory

    Returns:
        True if ritual executed successfully
    """
    template_name = "morning-brief" if ritual_type == "morning" else "evening-debrief"

    try:
        prompt = load_template(template_name, instance_dir)
    except FileNotFoundError as e:
        print(f"[rituals] {e}", file=sys.stderr)
        return False

    koan_root = os.environ.get("KOAN_ROOT", "")
    if not koan_root:
        print("[rituals] KOAN_ROOT not set", file=sys.stderr)
        return False

    try:
        cmd = build_full_command(
            prompt=prompt,
            allowed_tools=["Read", "Write", "Glob"],
            max_turns=7,
        )
        result = run_cli(
            cmd,
            cwd=koan_root,
            capture_output=True, text=True, timeout=90,
            check=False
        )
        if result.returncode == 0:
            print(f"[rituals] {ritual_type} ritual completed")
            output = strip_cli_noise(result.stdout.strip())
            if output:
                print(output)
            return True
        else:
            stderr = strip_cli_noise(result.stderr[:200])
            print(f"[rituals] {ritual_type} ritual failed: {stderr}", file=sys.stderr)
            return False
    except subprocess.TimeoutExpired:
        print(f"[rituals] {ritual_type} ritual timed out", file=sys.stderr)
        return False
    except Exception as e:
        print(f"[rituals] Error: {e}", file=sys.stderr)
        return False


def should_run_morning(run_num: int) -> bool:
    """Check if morning ritual should run.

    Args:
        run_num: Current run number (1-indexed)

    Returns:
        True if this is the first run
    """
    return run_num == 1


def should_run_evening(run_num: int, max_runs: int) -> bool:
    """Check if evening ritual should run.

    Args:
        run_num: Current run number (1-indexed)
        max_runs: Maximum configured runs

    Returns:
        True if this is the last run
    """
    return run_num == max_runs


def main():
    """CLI entry point."""
    if len(sys.argv) < 3:
        print("Usage: rituals.py <morning|evening> <instance_dir>", file=sys.stderr)
        sys.exit(1)

    ritual_type = sys.argv[1]
    instance_dir = Path(sys.argv[2])

    if ritual_type not in ("morning", "evening"):
        print(f"[rituals] Invalid ritual type: {ritual_type}. Use 'morning' or 'evening'.", file=sys.stderr)
        sys.exit(1)

    if not instance_dir.exists():
        print(f"[rituals] Instance directory not found: {instance_dir}", file=sys.stderr)
        sys.exit(1)

    success = run_ritual(ritual_type, instance_dir)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
