"""Shared helpers for GitHub-related skills.

Common utilities for skills that interact with GitHub PRs and issues:
- URL extraction and validation
- Project resolution
- Mission queuing
- Response formatting
- Unified skill handling
"""

import re
from typing import Callable, Optional, Tuple




def extract_github_url(args: str, url_type: str = "pr-or-issue") -> Optional[Tuple[str, Optional[str]]]:
    """Extract and validate a GitHub URL from command arguments.

    Args:
        args: Raw command arguments
        url_type: Expected URL type - "pr", "issue", or "pr-or-issue"

    Returns:
        Tuple of (url, remaining_context) where remaining_context is text after the URL,
        or None if no valid URL found

    Examples:
        >>> extract_github_url("https://github.com/o/r/pull/1 phase 1")
        ("https://github.com/o/r/pull/1", "phase 1")
    """
    if url_type == "pr":
        pattern = r'https?://github\.com/[^\s]+/pull/\d+'
    elif url_type == "issue":
        pattern = r'https?://github\.com/[^\s]+/issues/\d+'
    else:  # pr-or-issue
        pattern = r'https?://github\.com/[^\s]+/(?:pull|issues)/\d+'

    match = re.search(pattern, args)
    if not match:
        return None

    url = match.group(0).split("#")[0]  # Remove fragment
    context = args[match.end():].strip()
    return url, context if context else None


def resolve_project_for_repo(repo: str, owner: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """Resolve local project path and name for a GitHub repository.

    Args:
        repo: Repository name
        owner: Optional repository owner (for better matching)

    Returns:
        Tuple of (project_path, project_name) or (None, None) if not found
    """
    from app.utils import project_name_for_path, resolve_project_path

    project_path = resolve_project_path(repo, owner=owner)
    if not project_path:
        return None, None

    project_name = project_name_for_path(project_path)
    return project_path, project_name


def queue_github_mission(ctx, command: str, url: str, project_name: str, context: Optional[str] = None) -> None:
    """Queue a GitHub-related mission with consistent formatting.

    Args:
        ctx: Skill context
        command: Command name (e.g., "rebase", "review", "implement")
        url: GitHub URL
        project_name: Project name for tagging
        context: Optional additional context to append
    """
    from app.utils import insert_pending_mission

    mission_text = f"/{command} {url}"
    if context:
        mission_text += f" {context}"

    mission_entry = f"- [project:{project_name}] {mission_text}"
    missions_path = ctx.instance_dir / "missions.md"
    insert_pending_mission(missions_path, mission_entry)


def format_project_not_found_error(repo: str, owner: Optional[str] = None) -> str:
    """Format a consistent error message when project cannot be resolved.

    Args:
        repo: Repository name that couldn't be matched
        owner: Optional repository owner for more precise diagnostics

    Returns:
        Formatted error message with actionable suggestions
    """
    from app.utils import get_known_projects

    target = f"{owner}/{repo}" if owner else repo
    known = ", ".join(n for n, _ in get_known_projects()) or "none"
    lines = [
        f"\u274c Could not find local project for '{target}'.",
        f"Known projects: {known}",
    ]

    # Suggest closest match: find projects whose github_url has the same repo name
    if owner:
        suggestions = _find_repo_name_matches(repo)
        if suggestions:
            formatted = ", ".join(suggestions)
            lines.append(
                f"Possible match: {formatted} (same repo, different owner)."
            )

    lines.append(f"Tip: /add_project https://github.com/{target} to register it.")
    return "\n".join(lines)


def _find_repo_name_matches(repo: str) -> list:
    """Find projects whose github_url has the same repo name (different owner).

    Returns a list of "owner/repo" strings from matching projects.
    """
    repo_lower = repo.lower()
    matches = []
    try:
        from app.projects_config import load_projects_config
        from app.utils import KOAN_ROOT

        config = load_projects_config(str(KOAN_ROOT))
        if not config:
            return matches
        for _name, project in config.get("projects", {}).items():
            if not isinstance(project, dict):
                continue
            gh_url = project.get("github_url", "")
            if gh_url and "/" in gh_url:
                url_repo = gh_url.rsplit("/", 1)[1]
                if url_repo.lower() == repo_lower and gh_url not in matches:
                    matches.append(gh_url)
    except (OSError, ValueError, ImportError) as e:
        import sys
        print(f"[github_skill_helpers] repo name match failed: {e}", file=sys.stderr)
    return matches


def format_success_message(url_type: str, number: str, owner: str, repo: str, context: Optional[str] = None) -> str:
    """Format a consistent success message for queued missions.

    Args:
        url_type: Type of URL ("PR", "issue", etc.)
        number: PR or issue number
        owner: Repository owner
        repo: Repository name
        context: Optional context suffix

    Returns:
        Formatted success message
    """
    msg = f"{url_type} #{number} ({owner}/{repo})"
    if context:
        msg += f" — {context}"
    return msg


def handle_github_skill(
    ctx,
    command: str,
    url_type: str,
    parse_func: Callable[[str], Tuple[str, str, str]],
    success_prefix: str,
) -> str:
    """Unified handler for GitHub-based skills (review, implement, refactor).
    
    This consolidates the common pattern used by review, implement, and refactor skills:
    1. Extract and validate GitHub URL
    2. Parse URL to get owner/repo/number
    3. Resolve to local project
    4. Queue mission
    5. Return success message
    
    Args:
        ctx: Skill context
        command: Command name (e.g., "review", "implement", "refactor")
        url_type: URL type filter ("pr", "issue", or "pr-or-issue")
        parse_func: Function to parse the URL, returns (owner, repo, number) or (owner, repo, type, number)
        success_prefix: Prefix for success message (e.g., "Review queued")
        
    Returns:
        Success or error message string
    """
    args = ctx.args.strip()
    
    if not args:
        return _format_usage_message(command, url_type)
    
    # Extract URL from arguments
    result = extract_github_url(args, url_type=url_type)
    if not result:
        return _format_no_url_error(url_type)
    
    url, context = result
    
    # Parse URL
    try:
        parsed = parse_func(url)
    except ValueError as e:
        return f"\u274c {e}"
    
    # Handle different parse result formats
    if len(parsed) == 3:
        owner, repo, number = parsed
        type_label = "PR" if "pull" in url else "issue"
    else:
        owner, repo, url_type_result, number = parsed
        type_label = "PR" if url_type_result == "pull" else "issue"
    
    # Resolve project
    project_path, project_name = resolve_project_for_repo(repo, owner=owner)
    if not project_path:
        return format_project_not_found_error(repo, owner=owner)
    
    # Queue mission
    queue_github_mission(ctx, command, url, project_name, context)
    
    # Return success message
    return f"{success_prefix} for {format_success_message(type_label, number, owner, repo, context)}"


def _format_usage_message(command: str, url_type: str) -> str:
    """Format usage message for GitHub skills."""
    if url_type == "issue":
        examples = [
            f"Ex: /{command} https://github.com/sukria/koan/issues/42",
            f"Ex: /{command} https://github.com/sukria/koan/issues/42 phase 1 only",
        ]
        description = "issue"
    elif url_type == "pr":
        examples = [
            f"Ex: /{command} https://github.com/sukria/koan/pull/42",
        ]
        description = "PR"
    else:  # pr-or-issue
        examples = [
            f"Ex: /{command} https://github.com/sukria/koan/pull/42",
            f"Ex: /{command} https://github.com/sukria/koan/issues/42",
        ]
        description = "github-url"
    
    usage_line = f"Usage: /{command} <{description}> [context]"
    examples_text = "\n".join(examples)
    return f"{usage_line}\n{examples_text}\n\nQueues a {command} mission."


def _format_no_url_error(url_type: str) -> str:
    """Format error for missing GitHub URL."""
    if url_type == "issue":
        example = "https://github.com/owner/repo/issues/123"
    elif url_type == "pr":
        example = "https://github.com/owner/repo/pull/123"
    else:
        example = "https://github.com/owner/repo/pull/123"
    
    return f"\u274c No valid GitHub URL found.\nEx: {example}"
