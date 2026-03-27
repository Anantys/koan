"""Kōan deepplan skill -- queue a spec-first design mission."""


def handle(ctx):
    """Handle /deepplan command -- queue a mission to spec-design an idea.

    Usage:
        /deepplan                          -- usage help
        /deepplan <idea>                   -- deepplan for default project
        /deepplan <project> <idea>         -- deepplan for a specific project
        /deepplan <github-issue-url>       -- deepplan from a GitHub issue

    Queues a mission that invokes Claude to explore 2-3 design approaches,
    run a spec review loop, post the spec as a GitHub issue, and queue a
    follow-up /plan mission for human approval.

    When given a GitHub issue URL, the project is auto-detected from the
    repository and the issue title/body/comments are used as context.
    """
    args = ctx.args.strip()

    if not args:
        return (
            "Usage:\n"
            "  /deepplan <idea> -- spec-first design for default project\n"
            "  /deepplan <project> <idea> -- for a specific project\n"
            "  /deepplan <github-issue-url> -- from a GitHub issue\n\n"
            "Explores 2-3 design approaches, posts a spec as a GitHub issue,\n"
            "then queues /plan for your approval. Catches design flaws before\n"
            "any code is written."
        )

    # Check for GitHub issue URL
    issue_result = _parse_issue_url(args)
    if issue_result:
        return _queue_deepplan_from_issue(ctx, issue_result)

    # Parse optional project prefix
    project, idea = _parse_project_arg(args)

    if not idea:
        return "Please provide an idea. Ex: /deepplan Refactor the auth middleware"

    return _queue_deepplan(ctx, project, idea)


def _parse_issue_url(args):
    """Detect a GitHub issue URL in the arguments.

    Returns:
        Tuple of (url, owner, repo, issue_number) or None if no issue URL found.
    """
    from app.github_skill_helpers import extract_github_url

    result = extract_github_url(args, url_type="issue")
    if not result:
        return None

    url, _context = result

    from app.github_url_parser import parse_issue_url
    try:
        owner, repo, number = parse_issue_url(url)
    except ValueError:
        return None

    return url, owner, repo, number


def _queue_deepplan_from_issue(ctx, issue_result):
    """Queue a deepplan mission from a GitHub issue URL."""
    from app.utils import insert_pending_mission
    from app.github_skill_helpers import resolve_project_for_repo, format_project_not_found_error

    url, owner, repo, number = issue_result

    project_path, project_name = resolve_project_for_repo(repo, owner=owner)
    if not project_path:
        return format_project_not_found_error(repo, owner=owner)

    mission_entry = f"- [project:{project_name}] /deepplan {url}"
    missions_path = ctx.instance_dir / "missions.md"
    insert_pending_mission(missions_path, mission_entry)

    return f"\U0001f9e0 Deep plan queued from issue #{number} ({owner}/{repo}, project: {project_name})"


def _parse_project_arg(args):
    """Parse optional project prefix from args.

    Supports:
        /deepplan koan Fix the bug        -> ("koan", "Fix the bug")
        /deepplan [project:koan] Fix bug  -> ("koan", "Fix bug")
        /deepplan Fix the bug             -> (None, "Fix the bug")
    """
    from app.utils import parse_project, get_known_projects

    # Try [project:X] tag first
    project, cleaned = parse_project(args)
    if project:
        return project, cleaned

    # Try first word as project name
    parts = args.split(None, 1)
    if len(parts) < 2:
        return None, args

    candidate = parts[0].lower()
    known = get_known_projects()
    for name, _ in known:
        if name.lower() == candidate:
            return name, parts[1]

    return None, args


def _queue_deepplan(ctx, project_name, idea):
    """Queue a deepplan mission."""
    from app.utils import insert_pending_mission

    project_path = _resolve_project_path(project_name)
    if not project_path:
        from app.utils import get_known_projects
        known = ", ".join(n for n, _ in get_known_projects()) or "none"
        return f"Project '{project_name}' not found. Known: {known}"

    project_label = project_name or _project_name_for_path(project_path)

    mission_entry = f"- [project:{project_label}] /deepplan {idea}"
    missions_path = ctx.instance_dir / "missions.md"
    insert_pending_mission(missions_path, mission_entry)

    preview = idea[:100] + ('...' if len(idea) > 100 else '')
    return f"\U0001f9e0 Deep plan queued: {preview} (project: {project_label})"


def _resolve_project_path(project_name, fallback=False, owner=None):
    """Resolve project name to its local path."""
    from pathlib import Path
    from app.utils import get_known_projects, resolve_project_path

    if project_name:
        if owner:
            path = resolve_project_path(project_name, owner=owner)
            if path:
                return path
        for name, path in get_known_projects():
            if name.lower() == project_name.lower():
                return path
        for name, path in get_known_projects():
            if Path(path).name.lower() == project_name.lower():
                return path
        if not fallback:
            return None

    projects = get_known_projects()
    if projects:
        return projects[0][1]

    return ""


def _project_name_for_path(project_path):
    """Get project name from path, checking known projects first."""
    from app.utils import project_name_for_path
    return project_name_for_path(project_path)
