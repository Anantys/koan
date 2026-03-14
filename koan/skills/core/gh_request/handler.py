"""Koan gh_request skill — route natural-language GitHub requests.

When a user posts a free-form request via GitHub @mention (with
natural_language enabled), this skill classifies the intent and
queues the appropriate specific mission (fix, rebase, review, etc.).

This replaces the broken path where natural-language @mentions were
classified as e.g. /fix without the required issue URL, causing
"⚠️ /fix needs a GitHub issue URL to run."
"""

import re
from typing import Optional

from app.github_skill_helpers import (
    extract_github_url,
    queue_github_mission,
    resolve_project_for_repo,
    format_project_not_found_error,
)


def handle(ctx) -> Optional[str]:
    """Handle /gh_request — classify and dispatch a natural-language GitHub request.

    Usage:
        /gh_request https://github.com/owner/repo/pull/42 can you review this?
        /gh_request https://github.com/owner/repo/issues/10 please fix this bug
        /gh_request fix the login issue on repo koan
    """
    args = ctx.args.strip() if ctx.args else ""

    if not args:
        return (
            "Usage: /gh_request <github-url> <request>\n"
            "Ex: /gh_request https://github.com/owner/repo/pull/42 please review this\n\n"
            "Routes a natural-language request to the right skill (fix, rebase, review, etc.)."
        )

    # Extract GitHub URL if present
    url_result = extract_github_url(args, url_type="pr-or-issue")
    url = None
    request_text = args

    if url_result:
        url, remaining = url_result
        request_text = remaining if remaining else args.replace(url, "").strip()

    if not request_text:
        request_text = "handle this"

    # Resolve project from URL if we have one
    project_name = None
    if url:
        owner_repo = _parse_owner_repo(url)
        if owner_repo:
            owner, repo = owner_repo
            _, project_name = resolve_project_for_repo(repo, owner=owner)
            if not project_name:
                return format_project_not_found_error(repo, owner=owner)

    if not project_name:
        return (
            "\u274c Could not determine project. "
            "Include a GitHub URL so I can resolve the project.\n"
            "Ex: /gh_request https://github.com/owner/repo/pull/42 please review"
        )

    # Classify intent using the existing NLP classifier
    command, classified_context = _classify_request(request_text, project_name, url)

    if not command:
        # Classification failed or returned no match — queue as generic mission
        # The agent will handle it naturally via Claude
        mission_text = f"/gh_request {url} {request_text}" if url else f"/gh_request {request_text}"
        mission_entry = f"- [project:{project_name}] {mission_text}"
        from app.utils import insert_pending_mission
        missions_path = ctx.instance_dir / "missions.md"
        insert_pending_mission(missions_path, mission_entry)
        return f"Request queued for {project_name}: {request_text[:80]}"

    # Build the mission with the classified command
    mission_parts = [f"/{command}"]
    if url:
        mission_parts.append(url)
    if classified_context:
        mission_parts.append(classified_context)

    queue_github_mission(ctx, command, url or "", project_name, classified_context)

    url_info = f" ({url.split('/')[-1]})" if url else ""
    return f"/{command} queued for {project_name}{url_info}: {classified_context[:60]}" if classified_context else f"/{command} queued for {project_name}{url_info}"


def _classify_request(
    text: str,
    project_name: str,
    url: Optional[str],
) -> tuple:
    """Classify natural-language text into a bot command.

    Returns (command_name, context) or (None, "") on failure.
    """
    from app.github_command_handler import get_github_enabled_commands_with_descriptions
    from app.skills import build_registry

    registry = build_registry()
    commands = get_github_enabled_commands_with_descriptions(registry)
    if not commands:
        return None, ""

    # Resolve project path for Claude CLI
    from app.utils import get_known_projects

    project_path = None
    for name, path in get_known_projects():
        if name == project_name:
            project_path = path
            break
    if not project_path:
        return None, ""

    from app.github_intent import classify_intent

    result = classify_intent(text, commands, project_path)
    if not result or not result.get("command"):
        return None, ""

    command = result["command"]
    context = result.get("context", "")

    # Validate: if the classified command requires a URL type we don't have,
    # don't blindly forward — let the agent handle it as a generic request
    if command in ("fix", "implement") and url and "/issues/" not in url:
        # NLP classified as fix/implement but URL is a PR, not an issue
        # This is the exact bug we're fixing — don't forward to /fix
        return None, ""

    if command in ("rebase", "recreate", "review") and url and "/pull/" not in url:
        # Command needs a PR URL but we have an issue URL
        return None, ""

    return command, context


def _parse_owner_repo(url: str) -> Optional[tuple]:
    """Extract (owner, repo) from a GitHub URL."""
    match = re.search(
        r'github\.com/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)',
        url,
    )
    if not match:
        return None
    return match.group(1), match.group(2)
