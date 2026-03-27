"""Skill handler for /magic — creative project exploration via Claude."""

import random
import subprocess
from pathlib import Path
from typing import List, Tuple

from app.bridge_log import log
from app.project_explorer import (
    gather_git_activity,
    gather_project_structure,
    get_missions_context,
    get_projects,
)
from app.text_utils import clean_cli_response


def handle(ctx):
    """Explore a project and suggest creative improvement ideas.

    Picks a random project if no argument given, or targets a specific
    project when called as /magic <project>.
    """
    projects = get_projects()
    if not projects:
        return "No projects configured."

    target = ctx.args.strip().lower() if ctx.args else ""
    if target:
        name, path = _resolve_project(projects, target)
        if name is None:
            known = ", ".join(n for n, _ in projects)
            return f"Unknown project '{target}'. Known: {known}"
    else:
        name, path = random.choice(projects)

    if ctx.send_message:
        ctx.send_message(f"Exploring {name}...")

    git_activity = gather_git_activity(path)
    project_structure = gather_project_structure(path)
    missions_context = get_missions_context(ctx.instance_dir)

    soul = ""
    soul_path = ctx.instance_dir / "soul.md"
    if soul_path.exists():
        soul = soul_path.read_text()

    from app.prompts import load_skill_prompt
    prompt = load_skill_prompt(
        Path(__file__).parent,
        "magic-explore",
        SOUL=soul,
        PROJECT_NAME=name,
        GIT_ACTIVITY=git_activity,
        PROJECT_STRUCTURE=project_structure,
        MISSIONS_CONTEXT=missions_context,
    )

    try:
        from app.config import get_fast_reply_model
        from app.cli_provider import build_full_command
        fast_model = get_fast_reply_model()
        cmd = build_full_command(
            prompt=prompt,
            max_turns=1,
            model=fast_model or "",
        )
        from app.cli_exec import run_cli
        result = run_cli(
            cmd,
            capture_output=True, text=True, timeout=90,
            cwd=path,
        )
        if result.returncode == 0 and result.stdout.strip():
            return clean_cli_response(result.stdout.strip())
        else:
            if result.stderr:
                log("error", f"Magic Claude stderr: {result.stderr[:500]}")
            return f"Couldn't generate ideas for {name}. Try again later."
    except subprocess.TimeoutExpired:
        return "Timeout exploring. Try again."
    except Exception as e:
        log("error", f"Magic error: {e}")
        return "Error during exploration. Try again."


def _resolve_project(
    projects: List[Tuple[str, str]], target: str
) -> Tuple[str, str]:
    """Resolve a project by name. Returns (name, path) or (None, None)."""
    for name, path in projects:
        if name.lower() == target:
            return name, path
    return None, None
