"""
Kōan -- Contemplative session runner.

Manages contemplative sessions (probability roll, prompt building, CLI invocation).

CLI interface:
    python -m app.contemplative_runner should-run <chance>
    python -m app.contemplative_runner run --instance ... --project-name ... --session-info ...
"""

import random
import sys
from typing import List, Optional


def should_run_contemplative(chance: int) -> bool:
    """Roll the dice for a contemplative session.

    Args:
        chance: Probability percentage (0-100). E.g., 50 = 50% chance.

    Returns:
        True if the session should run.
    """
    if chance <= 0:
        return False
    if chance >= 100:
        return True
    return random.randint(0, 99) < chance


def build_contemplative_command(
    instance: str,
    project_name: str,
    session_info: str,
    extra_flags: Optional[List[str]] = None,
) -> List[str]:
    """Build the full CLI command for a contemplative session.

    Args:
        instance: Path to instance directory.
        project_name: Current project name.
        session_info: Context string for the session.
        extra_flags: Additional CLI flags (model, fallback, etc.).

    Returns:
        Complete command list ready for subprocess.run().
    """
    from app.prompt_builder import build_contemplative_prompt

    prompt = build_contemplative_prompt(
        instance=instance,
        project_name=project_name,
        session_info=session_info,
    )

    from app.cli_provider import build_full_command
    from app.config import get_contemplative_tools

    tools_str = get_contemplative_tools(project_name=project_name)
    allowed_tools = [t.strip() for t in tools_str.split(",") if t.strip()]

    cmd = build_full_command(
        prompt=prompt,
        allowed_tools=allowed_tools,
        max_turns=10,
    )
    if extra_flags:
        cmd.extend(extra_flags)

    return cmd


def get_contemplative_flags() -> List[str]:
    """Get CLI flags for contemplative role from config.

    Returns:
        List of CLI flag strings (may be empty).
    """
    from app.config import get_claude_flags_for_role

    flags_str = get_claude_flags_for_role("contemplative")
    if not flags_str.strip():
        return []
    return flags_str.split()


def run_contemplative_session(
    instance: str,
    project_name: str,
    session_info: str,
    cwd: Optional[str] = None,
    timeout: int = 300,
) -> dict:
    """Run a complete contemplative session.

    Args:
        instance: Path to instance directory.
        project_name: Current project name.
        session_info: Context string for the session.
        cwd: Working directory for the subprocess (defaults to instance).
        timeout: Maximum duration in seconds.

    Returns:
        Dict with keys: success (bool), output (str), error (str).
    """
    from app.claude_step import run_claude

    flags = get_contemplative_flags()
    cmd = build_contemplative_command(
        instance=instance,
        project_name=project_name,
        session_info=session_info,
        extra_flags=flags,
    )

    work_dir = cwd or instance
    return run_claude(cmd, cwd=work_dir, timeout=timeout)


def _cli_should_run(args: list) -> None:
    """CLI: python -m app.contemplative_runner should-run <chance>"""
    if len(args) < 1:
        print("Usage: python -m app.contemplative_runner should-run <chance>", file=sys.stderr)
        sys.exit(1)
    try:
        chance = int(args[0])
    except ValueError:
        print(f"Error: chance must be an integer, got '{args[0]}'", file=sys.stderr)
        sys.exit(1)
    if should_run_contemplative(chance):
        sys.exit(0)  # Should run
    else:
        sys.exit(1)  # Should not run


def _cli_run(args: list) -> None:
    """CLI: python -m app.contemplative_runner run --instance ... --project-name ... --session-info ..."""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--instance", required=True)
    parser.add_argument("--project-name", required=True)
    parser.add_argument("--session-info", required=True)
    parser.add_argument("--timeout", type=int, default=300)
    parsed = parser.parse_args(args)

    result = run_contemplative_session(
        instance=parsed.instance,
        project_name=parsed.project_name,
        session_info=parsed.session_info,
        timeout=parsed.timeout,
    )

    if result["output"]:
        print(result["output"])
    if not result["success"]:
        print(result["error"], file=sys.stderr)
        sys.exit(1)


def main() -> None:
    """CLI entry point."""
    if len(sys.argv) < 2:
        print("Usage: python -m app.contemplative_runner <should-run|run> [args]", file=sys.stderr)
        sys.exit(1)

    subcommand = sys.argv[1]
    remaining = sys.argv[2:]

    if subcommand == "should-run":
        _cli_should_run(remaining)
    elif subcommand == "run":
        _cli_run(remaining)
    else:
        print(f"Unknown subcommand: {subcommand}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
