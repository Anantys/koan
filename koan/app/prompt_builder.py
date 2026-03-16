"""Kōan — Prompt builder for the agent loop.

Handles agent prompt assembly (template + merge policy + deep research +
verbose mode) and contemplative prompt assembly.

Usage:
    PROMPT=$("$PYTHON" -m app.prompt_builder agent \
        --instance "$INSTANCE" \
        --project-name "$PROJECT_NAME" \
        --project-path "$PROJECT_PATH" \
        --run-num "$RUN_NUM" \
        --max-runs "$MAX_RUNS" \
        --autonomous-mode "${AUTONOMOUS_MODE:-implement}" \
        --focus-area "${FOCUS_AREA:-General autonomous work}" \
        --available-pct "${AVAILABLE_PCT:-50}" \
        --mission-title "$MISSION_TITLE")

    CONTEMPLATE_PROMPT=$("$PYTHON" -m app.prompt_builder contemplative \
        --instance "$INSTANCE" \
        --project-name "$PROJECT_NAME" \
        --session-info "$SESSION_INFO")
"""

import argparse
import os
import sys
from pathlib import Path


def _load_config_safe() -> dict:
    """Load config.yaml, returning empty dict on failure."""
    try:
        from app.utils import load_config
        return load_config()
    except (ImportError, OSError, ValueError):
        return {}


def _is_auto_merge_enabled(project_name: str) -> bool:
    """Check if auto-merge is enabled and has rules for the given project."""
    try:
        from app.config import get_auto_merge_config
        config = _load_config_safe()
        merge_cfg = get_auto_merge_config(config, project_name)
        return bool(merge_cfg.get("enabled", True) and merge_cfg.get("rules"))
    except (ImportError, OSError, ValueError, KeyError, TypeError):
        return False


def _get_branch_prefix() -> str:
    """Get the configured branch prefix."""
    try:
        from app.config import get_branch_prefix
        return get_branch_prefix()
    except (ImportError, OSError, ValueError):
        return "koan/"


def _get_merge_policy(project_name: str) -> str:
    """Return the merge policy section to append to the agent prompt."""
    from app.prompts import load_prompt

    prefix = _get_branch_prefix()
    if _is_auto_merge_enabled(project_name):
        return load_prompt("merge-policy-enabled", BRANCH_PREFIX=prefix)
    return load_prompt("merge-policy-disabled", BRANCH_PREFIX=prefix)


def _get_deep_research(instance: str, project_name: str, project_path: str) -> str:
    """Get deep research suggestions for DEEP mode."""
    try:
        from app.deep_research import DeepResearch
        research = DeepResearch(Path(instance), project_name, Path(project_path))
        suggestions = research.format_for_agent()
        if suggestions:
            return f"\n\n# Deep Research Analysis\n\n{suggestions}\n"
    except Exception as e:
        print(f"[prompt_builder] Deep research failed: {e}", file=sys.stderr)
    return ""


def _get_focus_section(instance: str) -> str:
    """Build the focus mode section if .koan-focus is active."""
    koan_root = str(Path(instance).parent)
    try:
        from app.focus_manager import check_focus
        state = check_focus(koan_root)
    except Exception as e:
        print(f"[prompt_builder] Focus check failed: {e}", file=sys.stderr)
        return ""

    if state is None:
        return ""

    from app.prompts import load_prompt

    remaining = state.remaining_display()
    return load_prompt("focus-mode", REMAINING=remaining)


def _get_submit_pr_section(project_path: str) -> str:
    """Return the submit-pull-request section (always included)."""
    from app.prompts import load_prompt

    return load_prompt("submit-pull-request", PROJECT_PATH=project_path)


def _get_staleness_section(instance: str, project_name: str) -> str:
    """Get staleness warning for the current project.

    Checks session outcome history and returns a warning if recent sessions
    for this project were non-productive. Cheap operation (local JSON read),
    so it's safe to call in every autonomous mode.
    """
    try:
        from app.session_tracker import get_staleness_warning
        warning = get_staleness_warning(instance, project_name)
        if warning:
            return f"\n\n# Session History Feedback\n\n{warning}\n"
    except Exception as e:
        print(f"[prompt_builder] Staleness check failed: {e}", file=sys.stderr)
    return ""


def _get_pr_feedback_section(project_path: str) -> str:
    """Get PR merge feedback for autonomous topic alignment.

    Summarizes which types of work get merged quickly vs. slowly,
    helping the agent choose high-alignment work. Uses gh CLI
    (network call), so kept lightweight with small limits.
    """
    try:
        from app.pr_feedback import get_alignment_summary
        summary = get_alignment_summary(project_path)
        if summary:
            return (
                f"\n\n# PR Merge Feedback\n\n"
                f"Recent merge patterns for your PRs on this project:\n"
                f"{summary}\n\n"
                f"Use this to guide autonomous topic selection — "
                f"prioritize work types that get merged quickly.\n"
            )
    except Exception as e:
        print(f"[prompt_builder] PR feedback failed: {e}", file=sys.stderr)
    return ""


def _get_drift_section(instance: str, project_name: str, project_path: str) -> str:
    """Get drift summary for the current project.

    Checks how many commits landed on main since the agent's last session
    on this project. Helps the agent avoid conflicts and stale assumptions.
    """
    try:
        from app.session_tracker import get_drift_summary
        summary = get_drift_summary(instance, project_name, project_path)
        if summary:
            return f"\n\n# Codebase Drift\n\n{summary}\n"
    except Exception as e:
        print(f"[prompt_builder] Drift check failed: {e}", file=sys.stderr)
    return ""


def _get_mission_type_section(mission_title: str) -> str:
    """Return type-specific guidance based on mission classification.

    Classifies the mission title into a work type (debug, implement, etc.)
    and loads the corresponding hint from mission-type-hints.md.
    Returns empty string for "general" type or when no mission is assigned.
    """
    if not mission_title:
        return ""

    try:
        from app.mission_classifier import classify_mission

        mission_type = classify_mission(mission_title)
        if mission_type == "general":
            return ""

        from app.prompts import load_prompt

        hints_text = load_prompt("mission-type-hints")

        # Extract the section for this type (## type\n\ncontent\n\n## next)
        import re

        pattern = rf"^## {re.escape(mission_type)}\n\n(.*?)(?=\n## |\Z)"
        match = re.search(pattern, hints_text, re.MULTILINE | re.DOTALL)
        if match:
            hint = match.group(1).strip()
            return (
                f"\n\n# Mission Approach Guidance\n\n"
                f"This looks like a **{mission_type}** mission. "
                f"{hint}\n"
            )
    except Exception as e:
        print(f"[prompt_builder] Mission type hint failed: {e}", file=sys.stderr)
    return ""


def _get_verification_gate_section(mission_title: str) -> str:
    """Return the verification gate section for mission-driven runs.

    Injects verification-before-completion rules that require fresh evidence
    before any success claim. Only included when executing a mission.
    """
    if not mission_title:
        return ""

    from app.prompts import load_prompt

    return load_prompt("verification-gate")


def _get_tdd_section(mission_title: str) -> str:
    """Return the TDD mode section if mission is tagged [tdd]."""
    from app.missions import extract_tdd_tag

    if not mission_title or not extract_tdd_tag(mission_title):
        return ""

    from app.prompts import load_prompt

    return load_prompt("tdd-mode")


def _get_verbose_section(instance: str) -> str:
    """Build the verbose mode section if .koan-verbose exists."""
    koan_root = str(Path(instance).parent)
    if not os.path.isfile(os.path.join(koan_root, ".koan-verbose")):
        return ""

    from app.prompts import load_prompt

    return load_prompt("verbose-mode", INSTANCE=instance)


def _get_security_flagging_section(mission_title: str, autonomous_mode: str) -> str:
    """Return the security vulnerability flagging section.

    Only included for mission-driven runs and review/implement autonomous
    modes — not for deep research or wait modes.
    """
    if not mission_title and autonomous_mode not in ("review", "implement"):
        return ""

    from app.prompts import load_prompt

    return load_prompt("security-flagging")


def build_agent_prompt(
    instance: str,
    project_name: str,
    project_path: str,
    run_num: int,
    max_runs: int,
    autonomous_mode: str,
    focus_area: str,
    available_pct: int,
    mission_title: str = "",
    spec_content: str = "",
) -> str:
    """Build the complete agent prompt from template + dynamic sections.

    Args:
        instance: Path to instance directory
        project_name: Current project name
        project_path: Path to project directory
        run_num: Current run number
        max_runs: Maximum runs per session
        autonomous_mode: Current mode (review/implement/deep)
        focus_area: Description of current focus
        available_pct: Budget percentage available
        mission_title: Mission title (empty for autonomous mode)
        spec_content: Pre-generated mission spec (empty to skip)

    Returns:
        Complete prompt string ready for Claude CLI
    """
    from app.prompts import load_prompt

    # Build mission instruction
    if mission_title:
        mission_instruction = (
            f"Your assigned mission is: **{mission_title}** "
            "The mission is already marked In Progress. "
            "Follow the Mission Execution Workflow below."
        )
    else:
        mission_instruction = (
            f"No specific mission assigned. Look for pending missions for "
            f"{project_name} in missions.md (check [project:{project_name}] "
            f"tags and ### project:{project_name} sub-headers). "
            "If none found, proceed to autonomous mode."
        )

    # Load template and substitute placeholders
    branch_prefix = _get_branch_prefix()
    prompt = load_prompt(
        "agent",
        INSTANCE=instance,
        PROJECT_PATH=project_path,
        PROJECT_NAME=project_name,
        RUN_NUM=str(run_num),
        MAX_RUNS=str(max_runs),
        AUTONOMOUS_MODE=autonomous_mode,
        FOCUS_AREA=focus_area,
        AVAILABLE_PCT=str(available_pct),
        MISSION_INSTRUCTION=mission_instruction,
        BRANCH_PREFIX=branch_prefix,
    )

    # Append mission spec (if generated)
    if spec_content and mission_title:
        prompt += (
            "\n\n# Mission Spec\n\n"
            "A spec was generated before implementation. Use it to anchor your work — "
            "follow the approach and stay within the defined scope. Reference key "
            "decisions in the PR description.\n\n"
            f"{spec_content}\n"
        )

    # Append mission type guidance (mission-driven runs only)
    prompt += _get_mission_type_section(mission_title)

    # Append merge policy
    prompt += _get_merge_policy(project_name)

    # Append security vulnerability flagging (mission-driven + review/implement)
    prompt += _get_security_flagging_section(mission_title, autonomous_mode)

    # Append submit-pull-request section
    prompt += _get_submit_pr_section(project_path)

    # Append staleness warning (all autonomous modes — cheap local read)
    if not mission_title:
        prompt += _get_staleness_section(instance, project_name)

    # Append drift detection (autonomous only — shows what changed on main)
    if not mission_title:
        prompt += _get_drift_section(instance, project_name, project_path)

    # Append PR merge feedback (autonomous only — helps topic alignment)
    if not mission_title and autonomous_mode in ("deep", "implement"):
        prompt += _get_pr_feedback_section(project_path)

    # Append deep research suggestions (DEEP mode, autonomous only)
    if autonomous_mode == "deep" and not mission_title:
        prompt += _get_deep_research(instance, project_name, project_path)

    # Append TDD mode section if mission is tagged [tdd]
    prompt += _get_tdd_section(mission_title)

    # Append verification gate for mission-driven runs
    prompt += _get_verification_gate_section(mission_title)

    # Append focus mode section if active
    prompt += _get_focus_section(instance)

    # Append verbose mode section if active
    prompt += _get_verbose_section(instance)

    return prompt


def build_contemplative_prompt(
    instance: str,
    project_name: str,
    session_info: str,
) -> str:
    """Build the contemplative session prompt from template.

    Args:
        instance: Path to instance directory
        project_name: Current project name
        session_info: Context about current session state

    Returns:
        Complete contemplative prompt string
    """
    from app.prompts import load_prompt

    return load_prompt(
        "contemplative",
        INSTANCE=instance,
        PROJECT_NAME=project_name,
        SESSION_INFO=session_info,
    )


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Build prompts for Kōan agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Agent prompt subcommand
    agent_parser = subparsers.add_parser("agent", help="Build agent mission prompt")
    agent_parser.add_argument("--instance", required=True)
    agent_parser.add_argument("--project-name", required=True)
    agent_parser.add_argument("--project-path", required=True)
    agent_parser.add_argument("--run-num", type=int, required=True)
    agent_parser.add_argument("--max-runs", type=int, required=True)
    agent_parser.add_argument("--autonomous-mode", default="implement")
    agent_parser.add_argument("--focus-area", default="General autonomous work")
    agent_parser.add_argument("--available-pct", type=int, default=50)
    agent_parser.add_argument("--mission-title", default="")

    # Contemplative prompt subcommand
    contemplate_parser = subparsers.add_parser(
        "contemplative", help="Build contemplative session prompt"
    )
    contemplate_parser.add_argument("--instance", required=True)
    contemplate_parser.add_argument("--project-name", required=True)
    contemplate_parser.add_argument("--session-info", required=True)

    args = parser.parse_args()

    if args.command == "agent":
        print(build_agent_prompt(
            instance=args.instance,
            project_name=args.project_name,
            project_path=args.project_path,
            run_num=args.run_num,
            max_runs=args.max_runs,
            autonomous_mode=args.autonomous_mode,
            focus_area=args.focus_area,
            available_pct=args.available_pct,
            mission_title=args.mission_title,
        ))
    elif args.command == "contemplative":
        print(build_contemplative_prompt(
            instance=args.instance,
            project_name=args.project_name,
            session_info=args.session_info,
        ))


if __name__ == "__main__":
    main()
