"""Koan -- Skill mission dispatch.

Detects skill-prefixed missions (e.g. "/plan Add dark mode") and builds
the corresponding CLI command for direct execution.

This replaces the verbose "run: `cd ... && python3 -m app.runner ...`"
pattern embedded in missions.md with a clean, human-readable format.

Mission format:
    /plan <idea>                        -> plan_runner --idea <idea>
    /plan <github-issue-url>            -> plan_runner --issue-url <url>
    /rebase <pr-url>                    -> rebase_pr <url>
    /recreate <pr-url>                  -> recreate_pr <url>
    /ai [project]                       -> ai_runner
    /check <url>                        -> check_runner <url>
    /claudemd                           -> claudemd_refresh <project-path>

Scoped skills:
    /core.plan <idea>                   -> same as /plan
    /namespace.skill <args>             -> resolved via skill registry
"""

import re
import sys
import threading
from pathlib import Path
from typing import List, Optional, Tuple

from app.github_url_parser import ISSUE_URL_PATTERN, PR_URL_PATTERN
from app.utils import is_known_project

# Module-level registry cache for the run process.
# bridge_state.py caches via _get_registry(), but translate_cli_skill_mission()
# (called from run.py) was rebuilding the registry from filesystem on every
# invocation.  This cache avoids that overhead.
_cached_registry = None
_cached_extra_dirs: Optional[tuple] = None
_registry_lock = threading.Lock()


# Mapping of skill command names to their CLI runner modules.
# Each entry: command_name -> (module_name, arg_builder_function_name)
_SKILL_RUNNERS = {
    "plan": "app.plan_runner",
    "implement": "skills.core.implement.implement_runner",
    "fix": "skills.core.fix.fix_runner",
    "rebase": "app.rebase_pr",
    "recreate": "app.recreate_pr",
    "review": "app.review_runner",
    "ai": "app.ai_runner",
    "check": "app.check_runner",
    "tech_debt": "skills.core.tech_debt.tech_debt_runner",
    "dead_code": "skills.core.dead_code.dead_code_runner",
    "profile": "skills.core.profile.profile_runner",
    "brainstorm": "skills.core.brainstorm.brainstorm_runner",
    "claudemd": "app.claudemd_refresh",
    "claude": "app.claudemd_refresh",
    "claude.md": "app.claudemd_refresh",
    "claude_md": "app.claudemd_refresh",
    "incident": "skills.core.incident.incident_runner",
}

_PROJECT_TAG_RE = re.compile(r"^\[projec?t:([a-zA-Z0-9_-]+)\]\s*")
_PROJECT_WORD_RE = re.compile(r"^[a-z][a-z0-9_-]*$")

# Compiled patterns for URL matching
_PR_URL_RE = re.compile(PR_URL_PATTERN)
_ISSUE_URL_RE = re.compile(ISSUE_URL_PATTERN)


def _strip_project_prefix(text: str) -> Tuple[str, str]:
    """Strip an optional project prefix from mission text.

    Handles two forms:
        - ``[project:koan] /plan Add dark mode``  -> ("koan", "/plan Add dark mode")
        - ``koan /plan Add dark mode``             -> ("koan", "/plan Add dark mode")
        - ``/plan Add dark mode``                  -> ("",     "/plan Add dark mode")

    Returns (project_id, remaining_text).
    """
    stripped = text.strip()

    # 1. [project:X] tag prefix
    tag_match = _PROJECT_TAG_RE.match(stripped)
    if tag_match:
        return tag_match.group(1), stripped[tag_match.end():].strip()

    # 2. Raw word prefix: "koan /plan ..."
    # Only accept known project names to avoid matching common English
    # words (e.g. "the /keyword ..." was incorrectly parsed as project="the").
    parts = stripped.split(None, 1)
    if (len(parts) >= 2
            and not parts[0].startswith("/")
            and parts[1].startswith("/")
            and _PROJECT_WORD_RE.match(parts[0])):
        candidate = parts[0]
        if is_known_project(candidate):
            return candidate, parts[1]

    # 3. No prefix
    return "", stripped


def is_skill_mission(mission_text: str) -> bool:
    """Check if a mission contains a /skill command.

    Handles optional project-id prefixes:
        - ``/plan Add dark mode``                  (plain)
        - ``[project:koan] /plan Add dark mode``   (tag prefix)
        - ``koan /plan Add dark mode``             (word prefix)

    Args:
        mission_text: The mission title text.

    Returns:
        True if the mission contains a /command.
    """
    _, remainder = _strip_project_prefix(mission_text)
    return remainder.startswith("/") and len(remainder) > 1


def parse_skill_mission(mission_text: str) -> Tuple[str, str, str]:
    """Parse a skill mission into (project_id, command, args).

    Handles optional project-id prefixes:
        - ``/plan Add dark mode``                  -> ("", "plan", "Add dark mode")
        - ``[project:koan] /plan Add dark mode``   -> ("koan", "plan", "Add dark mode")
        - ``koan /plan Add dark mode``             -> ("koan", "plan", "Add dark mode")

    Args:
        mission_text: e.g. "/plan Add dark mode" or "[project:koan] /core.plan Fix bug"

    Returns:
        (project_id, command, args) tuple. project_id is "" when no prefix.
        Command is normalized (no leading /).
        For scoped commands like "/core.plan", returns ("", "plan", args).
    """
    project_id, remainder = _strip_project_prefix(mission_text)

    if not remainder.startswith("/"):
        return project_id, "", remainder

    # Split into command and args
    parts = remainder[1:].split(None, 1)
    raw_command = parts[0]
    args = parts[1] if len(parts) > 1 else ""

    # Handle scoped commands: /core.plan -> plan, /namespace.skill -> namespace.skill
    if "." in raw_command:
        segments = raw_command.split(".", 1)
        scope = segments[0]
        skill_name = segments[1]
        # core scope is implicit — /core.plan == /plan
        if scope == "core":
            return project_id, skill_name, args
        # Keep the full scoped name for external skills
        return project_id, raw_command, args

    return project_id, raw_command, args


def build_skill_command(
    command: str,
    args: str,
    project_name: str,
    project_path: str,
    koan_root: str,
    instance_dir: str,
) -> Optional[List[str]]:
    """Build the CLI command list for a skill mission.

    Args:
        command: Skill command name (e.g. "plan", "rebase").
        args: Arguments string from the mission.
        project_name: Current project name.
        project_path: Path to the project directory.
        koan_root: Path to koan root directory.
        instance_dir: Path to instance directory.

    Returns:
        Command list ready for subprocess, or None if the skill
        is not recognized as a direct-dispatch skill.
    """
    from app.debug import debug_log

    runner_module = _SKILL_RUNNERS.get(command)
    if not runner_module:
        debug_log(
            f"[skill_dispatch] build_skill_command: no runner for '{command}' "
            f"(known: {', '.join(sorted(_SKILL_RUNNERS))})"
        )
        return None
    debug_log(f"[skill_dispatch] build_skill_command: '{command}' -> {runner_module}")

    python = sys.executable
    base_cmd = [python, "-m", runner_module]

    # Dispatch to command-specific builder
    _COMMAND_BUILDERS = {
        "brainstorm": lambda: _build_brainstorm_cmd(base_cmd, args, project_path),
        "plan": lambda: _build_plan_cmd(base_cmd, args, project_path),
        "implement": lambda: _build_implement_cmd(base_cmd, args, project_path),
        "fix": lambda: _build_implement_cmd(base_cmd, args, project_path),
        "rebase": lambda: _build_pr_url_cmd(base_cmd, args, project_path),
        "recreate": lambda: _build_pr_url_cmd(base_cmd, args, project_path),
        "review": lambda: _build_review_cmd(base_cmd, args, project_path),
        "ai": lambda: _build_ai_cmd(base_cmd, project_name, project_path, instance_dir),
        "check": lambda: _build_check_cmd(base_cmd, args, instance_dir, koan_root),
        "tech_debt": lambda: _build_tech_debt_cmd(
            base_cmd, project_name, project_path, instance_dir,
        ),
        "dead_code": lambda: _build_dead_code_cmd(
            base_cmd, project_name, project_path, instance_dir,
        ),
        "profile": lambda: _build_profile_cmd(base_cmd, args, project_path, instance_dir),
        "claudemd": lambda: _build_claudemd_cmd(base_cmd, project_name, project_path),
        "claude": lambda: _build_claudemd_cmd(base_cmd, project_name, project_path),
        "claude.md": lambda: _build_claudemd_cmd(base_cmd, project_name, project_path),
        "claude_md": lambda: _build_claudemd_cmd(base_cmd, project_name, project_path),
        "incident": lambda: _build_incident_cmd(base_cmd, args, project_path, instance_dir),
    }

    builder = _COMMAND_BUILDERS.get(command)
    return builder() if builder else None


def _extract_issue_url_and_context(args: str) -> Optional[Tuple[str, str]]:
    """Extract issue URL and remaining context from arguments.
    
    Args:
        args: Argument string potentially containing an issue URL.
        
    Returns:
        Tuple of (issue_url, context) or None if no URL found.
        Context is the text after the URL, stripped.
    """
    issue_match = _ISSUE_URL_RE.search(args)
    if not issue_match:
        return None
    
    issue_url = issue_match.group(0)
    context = args[issue_match.end():].strip()
    return issue_url, context


def _extract_pr_or_issue_url_and_context(args: str) -> Optional[Tuple[str, str]]:
    """Extract PR or issue URL and remaining context from arguments.

    Unlike _extract_issue_url_and_context (issue-only), this matches
    both /issues/ and /pull/ URLs. Used by /plan which can iterate on
    either type.

    Returns:
        Tuple of (url, context) or None if no URL found.
    """
    match = re.search(
        r'https?://github\.com/[^/]+/[^/]+/(?:issues|pull)/\d+',
        args,
    )
    if not match:
        return None
    url = match.group(0)
    context = args[match.end():].strip()
    return url, context


def _build_brainstorm_cmd(
    base_cmd: List[str], args: str, project_path: str,
) -> List[str]:
    """Build brainstorm_runner command."""
    cmd = base_cmd + ["--project-path", project_path]

    # Extract --tag if present
    tag_match = re.search(r'--tag\s+(\S+)', args)
    if tag_match:
        cmd.extend(["--tag", tag_match.group(1)])
        # Remove --tag from args to get the topic
        topic = args[:tag_match.start()].rstrip() + args[tag_match.end():]
        topic = topic.strip()
    else:
        topic = args.strip()

    cmd.extend(["--topic", topic])
    return cmd


def _build_plan_cmd(
    base_cmd: List[str], args: str, project_path: str,
) -> List[str]:
    """Build plan_runner command."""
    cmd = base_cmd + ["--project-path", project_path]

    # Detect issue or PR URL vs free-text idea.
    # PR URLs are accepted: GitHub's issues API works for PRs too,
    # so plan_runner can fetch PR title/body/comments the same way.
    url_and_context = _extract_pr_or_issue_url_and_context(args)
    if url_and_context:
        issue_url, context = url_and_context
        cmd.extend(["--issue-url", issue_url])
        if context:
            cmd.extend(["--context", context])
    else:
        cmd.extend(["--idea", args])

    return cmd


def _build_implement_cmd(
    base_cmd: List[str], args: str, project_path: str,
) -> Optional[List[str]]:
    """Build implement_runner command.

    Expects an issue URL and optional context text after it.
    Example args: "https://github.com/o/r/issues/42 Phase 1 to 3"
    """
    url_and_context = _extract_issue_url_and_context(args)
    if not url_and_context:
        return None
    
    issue_url, context = url_and_context
    cmd = base_cmd + [
        "--project-path", project_path,
        "--issue-url", issue_url,
    ]

    if context:
        cmd.extend(["--context", context])

    return cmd


def _build_pr_url_cmd(
    base_cmd: List[str], args: str, project_path: str,
) -> Optional[List[str]]:
    """Build command for PR-URL-based skills (rebase, recreate)."""
    url_match = _PR_URL_RE.search(args)
    if not url_match:
        return None
    return base_cmd + [url_match.group(0), "--project-path", project_path]


def _build_review_cmd(
    base_cmd: List[str], args: str, project_path: str,
) -> Optional[List[str]]:
    """Build review_runner command, passing --architecture if present."""
    url_match = _PR_URL_RE.search(args)
    if not url_match:
        return None
    cmd = base_cmd + [url_match.group(0), "--project-path", project_path]
    if "--architecture" in args:
        cmd.append("--architecture")
    return cmd


def _build_ai_cmd(
    base_cmd: List[str],
    project_name: str,
    project_path: str,
    instance_dir: str,
) -> List[str]:
    """Build ai_runner command."""
    return base_cmd + [
        "--project-path", project_path,
        "--project-name", project_name,
        "--instance-dir", instance_dir,
    ]


def _build_check_cmd(
    base_cmd: List[str],
    args: str,
    instance_dir: str,
    koan_root: str,
) -> Optional[List[str]]:
    """Build check_runner command."""
    # Extract URL from args
    url_match = _PR_URL_RE.search(args) or _ISSUE_URL_RE.search(args)
    if not url_match:
        return None
    return base_cmd + [
        url_match.group(0),
        "--instance-dir", instance_dir,
        "--koan-root", koan_root,
    ]


def _build_tech_debt_cmd(
    base_cmd: List[str],
    project_name: str,
    project_path: str,
    instance_dir: str,
) -> List[str]:
    """Build tech_debt_runner command."""
    return base_cmd + [
        "--project-path", project_path,
        "--project-name", project_name,
        "--instance-dir", instance_dir,
    ]


def _build_dead_code_cmd(
    base_cmd: List[str],
    project_name: str,
    project_path: str,
    instance_dir: str,
) -> List[str]:
    """Build dead_code_runner command."""
    return base_cmd + [
        "--project-path", project_path,
        "--project-name", project_name,
        "--instance-dir", instance_dir,
    ]


def _build_profile_cmd(
    base_cmd: List[str],
    args: str,
    project_path: str,
    instance_dir: str,
) -> List[str]:
    """Build profile_runner command."""
    cmd = base_cmd + ["--project-path", project_path, "--instance-dir", instance_dir]
    # Optional PR URL
    url_match = _PR_URL_RE.search(args)
    if url_match:
        cmd.extend(["--pr-url", url_match.group(0)])
    return cmd


def _build_claudemd_cmd(
    base_cmd: List[str],
    project_name: str,
    project_path: str,
) -> List[str]:
    """Build claudemd_refresh command."""
    return base_cmd + [
        project_path,
        "--project-name", project_name,
    ]


def _build_incident_cmd(
    base_cmd: List[str],
    args: str,
    project_path: str,
    instance_dir: str,
) -> List[str]:
    """Build incident_runner command.

    The error text is passed via --error-file (temp file) to avoid
    shell escaping issues with stack traces.
    """
    import tempfile

    cmd = base_cmd + ["--project-path", project_path, "--instance-dir", instance_dir]

    # Write error text to a temp file to avoid shell escaping issues
    if args.strip():
        fd, path = tempfile.mkstemp(prefix="koan-incident-", suffix=".txt")
        with open(fd, "w", encoding="utf-8") as f:
            f.write(args)
        cmd.extend(["--error-file", path])

    return cmd


def cleanup_skill_temp_files(skill_cmd: List[str]) -> None:
    """Remove temp files created by skill command builders.

    Currently handles:
    - ``--error-file`` temp files from ``_build_incident_cmd()``

    Safe to call on any skill_cmd — silently skips if no temp files found.
    """
    import os

    for i, token in enumerate(skill_cmd):
        if token == "--error-file" and i + 1 < len(skill_cmd):
            path = skill_cmd[i + 1]
            # Only remove files we created (koan-incident-* in temp dir)
            if "/koan-incident-" in path:
                try:
                    os.unlink(path)
                except OSError:
                    pass


def validate_skill_args(command: str, args: str) -> Optional[str]:
    """Return a human-readable error if args are invalid for a known command.

    Returns None if the command is unknown (caller should handle that case)
    or if the args are valid.

    Note: validation mirrors the URL checks in _build_pr_url_cmd,
    _build_implement_cmd, and _build_check_cmd. Update both when
    adding new URL-requiring skills.
    """
    if command not in _SKILL_RUNNERS:
        return None

    if command in ("rebase", "recreate", "review"):
        if not _PR_URL_RE.search(args):
            return (
                f"/{command} requires a PR URL "
                f"(e.g. https://github.com/owner/repo/pull/123)"
            )
    elif command in ("implement", "fix"):
        if not _ISSUE_URL_RE.search(args):
            return (
                f"/{command} requires an issue URL "
                f"(e.g. https://github.com/owner/repo/issues/42)"
            )
    elif command == "check":
        if not (_PR_URL_RE.search(args) or _ISSUE_URL_RE.search(args)):
            return "/check requires a GitHub URL (PR or issue)"

    return None


def translate_cli_skill_mission(
    mission_text: str,
    koan_root: Path,
    instance_dir: Path,
) -> Optional[str]:
    """If the mission is a cli_skill mission, return the translated CLI task text.

    A cli_skill mission is one where the referenced skill has a ``cli_skill``
    field set in its SKILL.md. The Kōan slash command is replaced with the
    provider slash command declared in that field.

    Example:
        SKILL.md has ``cli_skill: my-tool``
        Mission: ``[project:foo] /group.myskill do something``
        Returns: ``/my-tool do something``

    Returns:
        Translated task text (e.g. "/my-tool do something"), or None if the
        mission is not a cli_skill mission or the skill cannot be found.
    """
    from app.debug import debug_log
    from app.skills import build_registry

    _, bare = _strip_project_prefix(mission_text)
    if not bare.startswith("/"):
        return None

    parts = bare[1:].split(None, 1)  # ["group.myskill", "do something"]
    command_part = parts[0]
    args = parts[1] if len(parts) > 1 else ""

    # Only handle scoped commands (scope.name) — unscoped ones go to _SKILL_RUNNERS
    if "." not in command_part:
        return None

    segments = command_part.split(".", 1)
    scope = segments[0]
    name = segments[1]

    # Skip core scope (handled by _SKILL_RUNNERS)
    if scope == "core":
        return None

    # Look up skill in registry — cached to avoid rebuilding from filesystem
    # on every mission check.  Lock protects against concurrent rebuild races
    # when multiple missions start simultaneously.
    global _cached_registry, _cached_extra_dirs
    instance_skills_dir = instance_dir / "skills"
    extra = tuple(p for p in [instance_skills_dir] if p.is_dir())
    with _registry_lock:
        if _cached_registry is None or extra != _cached_extra_dirs:
            _cached_registry = build_registry(list(extra))
            _cached_extra_dirs = extra
        registry = _cached_registry

    skill = registry.get(scope, name)
    if skill is None or not skill.cli_skill:
        return None

    # Translate: replace koan skill name with CLI provider skill name
    translated = f"/{skill.cli_skill}"
    if args:
        translated += f" {args}"

    debug_log(
        f"[skill_dispatch] translate_cli_skill: '{command_part}' -> '{skill.cli_skill}'"
        f" args='{args[:80]}'"
    )
    return translated


def dispatch_skill_mission(
    mission_text: str,
    project_name: str,
    project_path: str,
    koan_root: str,
    instance_dir: str,
) -> Optional[List[str]]:
    """High-level entry point: parse + build command for a skill mission.

    Args:
        mission_text: The mission title (e.g. "/plan Add dark mode").
        project_name: Current project name.
        project_path: Path to the project directory.
        koan_root: Path to koan root.
        instance_dir: Path to instance directory.

    Returns:
        Command list ready for subprocess, or None if not a skill mission
        or the skill is not recognized.
    """
    from app.debug import debug_log

    preview = f"{mission_text[:100]}..." if len(mission_text) > 100 else mission_text
    debug_log(f"[skill_dispatch] dispatch: mission_text='{preview}'")

    if not is_skill_mission(mission_text):
        debug_log("[skill_dispatch] dispatch: regular mission (no /command prefix), proceeding to Claude")
        return None

    parsed_project, command, args = parse_skill_mission(mission_text)
    debug_log(
        f"[skill_dispatch] dispatch: parsed project='{parsed_project}' "
        f"command='{command}' args='{args[:80]}'"
    )
    if not command:
        return None

    # Use parsed project-id as fallback when caller's project_name is empty
    effective_project = project_name or parsed_project

    result = build_skill_command(
        command=command,
        args=args,
        project_name=effective_project,
        project_path=project_path,
        koan_root=koan_root,
        instance_dir=instance_dir,
    )
    if result:
        debug_log(f"[skill_dispatch] dispatch: built command: {' '.join(result[:5])}")
    else:
        debug_log("[skill_dispatch] dispatch: build_skill_command returned None")
    return result
