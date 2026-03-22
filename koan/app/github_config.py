"""GitHub notification configuration helpers.

Reads GitHub-specific settings from config.yaml (global) and projects.yaml
(per-project override) for the notification-driven commands feature.

Config schema in config.yaml:
    github:
      nickname: "koan-bot"
      commands_enabled: true
      authorized_users: ["*"]
      reply_authorized_users: ["alice", "bob"]  # optional
      reply_rate_limit: 5
      max_age_hours: 24
      reply_enabled: false
      check_interval_seconds: 60

Per-project override in projects.yaml:
    projects:
      myproject:
        github:
          authorized_users: ["alice", "bob"]
          reply_authorized_users: ["alice", "bob"]
"""

from typing import List, Optional


def get_github_nickname(config: dict) -> str:
    """Get the bot's GitHub @mention nickname from config.yaml.

    Returns empty string if not configured.
    """
    github = config.get("github") or {}
    return str(github.get("nickname", "")).strip()


def get_github_commands_enabled(config: dict) -> bool:
    """Check if GitHub notification commands are enabled in config.yaml."""
    github = config.get("github") or {}
    return bool(github.get("commands_enabled", False))


def get_github_authorized_users(config: dict, project_name: Optional[str] = None,
                                 projects_config: Optional[dict] = None) -> List[str]:
    """Get the list of authorized GitHub users.

    If project_name and projects_config are provided, checks for per-project
    override first. Falls back to global config.yaml setting.

    Returns ["*"] for wildcard (all users), or a list of GitHub usernames.
    Returns empty list if not configured.
    """
    # Check per-project override first
    if project_name and projects_config:
        from app.projects_config import get_project_github_authorized_users
        project_users = get_project_github_authorized_users(projects_config, project_name)
        if project_users:
            return project_users

    # Fall back to global config.yaml
    github = config.get("github") or {}
    users = github.get("authorized_users", [])
    return users if isinstance(users, list) else []


def get_github_natural_language(config: dict, project_name: Optional[str] = None,
                                projects_config: Optional[dict] = None) -> bool:
    """Check if natural-language intent parsing is enabled for GitHub @mentions.

    When enabled, unrecognized commands are sent to Claude for intent
    classification before falling back to error/reply paths.

    Checks per-project override first (via projects.yaml), then falls back
    to global config.yaml setting.  Default: False.
    """
    # Check per-project override first
    if project_name and projects_config:
        from app.projects_config import get_project_github_natural_language
        project_value = get_project_github_natural_language(projects_config, project_name)
        if project_value is not None:
            return project_value

    # Fall back to global config.yaml
    github = config.get("github") or {}
    return bool(github.get("natural_language", False))


def get_github_reply_enabled(config: dict) -> bool:
    """Check if AI-powered replies to non-command @mentions are enabled.

    When enabled, the bot will generate contextual replies to questions
    from authorized users, rather than only responding to known commands.
    """
    github = config.get("github") or {}
    return bool(github.get("reply_enabled", False))


def get_github_reply_authorized_users(
    config: dict,
    project_name: Optional[str] = None,
    projects_config: Optional[dict] = None,
) -> Optional[List[str]]:
    """Get the list of authorized GitHub users for reply generation.

    Checks per-project github.reply_authorized_users first, then global
    github.reply_authorized_users. Returns None when not configured so callers
    can fall back to command-level authorized_users for backward compatibility.
    """
    # Check per-project override first
    if project_name and projects_config:
        from app.projects_config import get_project_github_reply_authorized_users
        project_users = get_project_github_reply_authorized_users(
            projects_config, project_name
        )
        if project_users is not None:
            return project_users

    # Fall back to global config.yaml
    github = config.get("github") or {}
    users = github.get("reply_authorized_users")
    if users is None:
        return None
    return users if isinstance(users, list) else None


def get_github_reply_rate_limit(config: dict) -> int:
    """Get max replies per user per hour for GitHub reply flow.

    Default: 5. Values below 1 are floored to 1.
    """
    github = config.get("github") or {}
    try:
        val = int(github.get("reply_rate_limit", 5))
        return max(1, val)
    except (ValueError, TypeError):
        return 5


def get_github_max_age_hours(config: dict) -> int:
    """Get max age in hours for processing notifications.

    Notifications older than this are ignored (stale protection).
    Default: 24 hours.
    """
    github = config.get("github") or {}
    try:
        return int(github.get("max_age_hours", 24))
    except (ValueError, TypeError):
        return 24


def get_github_check_interval(config: dict) -> int:
    """Get the minimum interval in seconds between notification checks.

    Controls throttling of GitHub API calls for notification polling.
    Default: 60 seconds.
    """
    github = config.get("github") or {}
    try:
        val = int(github.get("check_interval_seconds", 60))
        return max(10, val)  # Floor at 10s to prevent API abuse
    except (ValueError, TypeError):
        return 60


def get_github_max_check_interval(config: dict) -> int:
    """Get the maximum backoff interval in seconds for notification checks.

    When consecutive checks find no notifications, the interval grows
    exponentially up to this cap.  Default: 180 seconds (3 minutes).
    """
    github = config.get("github") or {}
    try:
        val = int(github.get("max_check_interval_seconds", 180))
        return max(30, val)  # Floor at 30s — below that backoff is pointless
    except (ValueError, TypeError):
        return 180


def get_github_subscribe_enabled(config: dict) -> bool:
    """Check if thread subscription monitoring is enabled.

    When enabled, Kōan monitors GitHub threads for new comments and
    queues /reply missions for actionable ones.
    """
    github = config.get("github") or {}
    return bool(github.get("subscribe_enabled", False))


def get_github_subscribe_max_per_cycle(config: dict) -> int:
    """Max subscription notifications to process per polling cycle.

    Prevents excessive API usage when many threads are active.
    Default: 5.
    """
    github = config.get("github") or {}
    try:
        return max(1, int(github.get("subscribe_max_per_cycle", 5)))
    except (ValueError, TypeError):
        return 5


def validate_github_config(config: dict) -> Optional[str]:
    """Validate GitHub configuration at startup.

    Returns an error message if config is invalid, or None if valid.
    """
    if not get_github_commands_enabled(config):
        return None  # Feature disabled, no validation needed

    nickname = get_github_nickname(config)
    if not nickname:
        return "GitHub commands enabled but 'github.nickname' is not set in config.yaml"

    return None
