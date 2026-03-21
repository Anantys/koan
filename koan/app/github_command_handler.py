"""GitHub command handler — bridges notifications to missions and replies.

Orchestrates the full flow from a GitHub @mention notification to either:
- A queued mission in missions.md (for recognized commands)
- A direct AI-generated reply (for questions/requests from authorized users)

Command flow:
1. Parse comment → extract command
2. Validate command → check skill has github_enabled
3. Check permissions → verify user is authorized
4. Add reaction → mark as processed (👍)
5. Build mission → format with project tag
6. Insert mission → write to missions.md

Reply flow (when reply_enabled=true and command not recognized):
1. Verify user is authorized
2. Fetch issue/PR thread context
3. Generate AI reply via Claude CLI
4. Post reply as GitHub comment
"""

import logging
import re
from typing import List, Optional, Tuple

from app.bounded_set import BoundedSet
from app.github_config import (
    get_github_authorized_users,
    get_github_natural_language,
    get_github_nickname,
    get_github_reply_enabled,
    get_github_subscribe_enabled,
    get_github_subscribe_max_per_cycle,
)
from app.github_notifications import (
    add_reaction,
    api_url_to_web_url,
    check_already_processed,
    check_user_permission,
    find_mention_in_thread,
    get_comment_from_notification,
    is_notification_stale,
    is_self_mention,
    mark_notification_read,
    parse_mention_command,
)
from app.skills import SkillRegistry

log = logging.getLogger(__name__)

# Track error replies to avoid duplicate error messages per comment.
# Bounded: FIFO eviction when limit is reached (oldest entries removed first).
_MAX_TRACKED_ENTRIES = 10000
_error_replies: BoundedSet = BoundedSet(maxlen=_MAX_TRACKED_ENTRIES)


def _quarantine_github_mission(text: str, reason: str, author: str):
    """Write a flagged GitHub mission to the quarantine file."""
    import os
    from datetime import datetime
    from pathlib import Path

    koan_root = os.environ.get("KOAN_ROOT", "")
    if not koan_root:
        return
    quarantine_path = Path(koan_root) / "instance" / "missions-quarantine.md"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"- 🛡️ [{timestamp}] (github/@{author}) {reason}: {text[:500]}\n"
    try:
        with open(quarantine_path, "a") as f:
            f.write(entry)
    except OSError:
        log.warning("GitHub: failed to write quarantine entry: %s", reason)


def validate_command(command_name: str, registry: SkillRegistry) -> Optional[object]:
    """Check if a command maps to a skill with github_enabled.

    Args:
        command_name: The command to validate (e.g., "rebase").
        registry: The skills registry.

    Returns:
        The Skill object if valid, or None.
    """
    skill = registry.find_by_command(command_name)
    if skill is None:
        return None
    if not skill.github_enabled:
        return None
    return skill


def get_github_enabled_commands(registry: SkillRegistry) -> List[str]:
    """Get list of command names that are github_enabled.

    Returns sorted, deduplicated list of primary command names.
    """
    commands = set()
    for skill in registry.list_all():
        if skill.github_enabled:
            for cmd in skill.commands:
                commands.add(cmd.name)
    return sorted(commands)


def get_github_enabled_commands_with_descriptions(
    registry: SkillRegistry,
) -> List[Tuple[str, str]]:
    """Get github-enabled commands with their descriptions.

    Returns sorted list of (command_name, description) tuples.
    Only includes primary command names (not aliases).
    """
    commands: dict = {}
    for skill in registry.list_all():
        if skill.github_enabled:
            for cmd in skill.commands:
                if cmd.name not in commands:
                    commands[cmd.name] = cmd.description or skill.description
    return sorted(commands.items())


def format_help_message(
    invalid_command: str,
    registry: SkillRegistry,
    bot_username: str,
) -> str:
    """Build a help message listing available GitHub commands.

    Args:
        invalid_command: The command that was not recognized.
        registry: Skills registry.
        bot_username: The bot's GitHub username (for usage examples).

    Returns:
        A formatted markdown help message for GitHub comments.
    """
    commands = get_github_enabled_commands_with_descriptions(registry)

    suggestion = registry.suggest_command(invalid_command)
    hint = f" Did you mean `{suggestion}`?" if suggestion else ""
    lines = [f"Unknown command `{invalid_command}`.{hint} Here are the commands I support:\n"]
    for name, description in commands:
        lines.append(f"- `@{bot_username} {name}` — {description}")

    lines.append(f"\nUsage: `@{bot_username} <command>` in any PR or issue comment.")
    return "\n".join(lines)


def format_help_list_message(
    registry: SkillRegistry,
    bot_username: str,
) -> str:
    """Build a clean help message listing available GitHub commands.

    Unlike format_help_message, this does NOT prefix with "Unknown command".
    Used when the user explicitly asks for help via ``@bot help``.

    Args:
        registry: Skills registry.
        bot_username: The bot's GitHub username (for usage examples).

    Returns:
        A formatted markdown help message for GitHub comments.
    """
    commands = get_github_enabled_commands_with_descriptions(registry)

    lines = ["Here are the commands I support:\n"]
    for name, description in commands:
        lines.append(f"- `@{bot_username} {name}` — {description}")

    lines.append(f"- `@{bot_username} help` — Show this help message")
    lines.append(f"\nUsage: `@{bot_username} <command>` in any PR or issue comment.")
    return "\n".join(lines)


def _post_help_reply(
    owner: str,
    repo: str,
    issue_number: str,
    help_message: str,
) -> bool:
    """Post a help reply to a GitHub issue/PR comment thread.

    Args:
        owner: Repository owner.
        repo: Repository name.
        issue_number: Issue or PR number.
        help_message: The help message body.

    Returns:
        True if posted successfully.
    """
    from app.github import api

    try:
        api(
            f"repos/{owner}/{repo}/issues/{issue_number}/comments",
            method="POST",
            extra_args=["-f", f"body={help_message}"],
        )
        return True
    except RuntimeError:
        log.warning("GitHub: failed to post help reply on %s/%s#%s", owner, repo, issue_number)
        return False


def _handle_help_command(
    notification: dict,
    comment: dict,
    registry: SkillRegistry,
    bot_username: str,
    owner: str,
    repo: str,
) -> bool:
    """Handle the built-in 'help' command — reply with available commands list.

    Posts a help comment, reacts with 👍, and marks notification as read.

    Args:
        notification: Notification dict.
        comment: Comment dict.
        registry: Skills registry.
        bot_username: Bot's GitHub username.
        owner: Repository owner.
        repo: Repository name.

    Returns:
        True if help was posted successfully.
    """
    issue_number = extract_issue_number_from_notification(notification)
    if not issue_number:
        log.debug("GitHub help: could not extract issue number")
        mark_notification_read(str(notification.get("id", "")))
        return False

    help_msg = format_help_list_message(registry, bot_username)
    if not _post_help_reply(owner, repo, issue_number, help_msg):
        mark_notification_read(str(notification.get("id", "")))
        return False

    # React and mark as read
    comment_id = str(comment.get("id", ""))
    comment_api_url = comment.get("url", "")
    add_reaction(owner, repo, comment_id, emoji="eyes",
                 comment_api_url=comment_api_url)
    mark_notification_read(str(notification.get("id", "")))

    log.info("GitHub: posted help reply on %s/%s#%s", owner, repo, issue_number)
    return True


def _resolve_project_from_url(url: str) -> Optional[str]:
    """Resolve project name from a GitHub URL's owner/repo.

    Parses the URL to extract owner and repo, then looks up the
    corresponding project. Returns the project name or None if the
    URL cannot be parsed or the repo is not a known project.
    """
    match = re.search(
        r'https?://github\.com/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)',
        url,
    )
    if not match:
        return None

    owner, repo = match.group(1), match.group(2)

    from app.utils import project_name_for_path, resolve_project_path

    project_path = resolve_project_path(repo, owner=owner)
    if not project_path:
        return None

    return project_name_for_path(project_path)


def _extract_url_from_context(context: str) -> Optional[Tuple[str, str]]:
    """Extract URL from context text if present.
    
    Args:
        context: Context text that may contain a URL
        
    Returns:
        Tuple of (url, remaining_context) or None if no URL found
    """
    # Require /pull/N or /issues/N path — bare repo URLs must not match
    url_match = re.search(
        r'https?://github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/(?:pull|issues)/\d+',
        context,
    )
    if not url_match:
        return None
    
    url = url_match.group(0)
    # Remove URL from context
    remaining = context[:url_match.start()].strip() + " " + context[url_match.end():].strip()
    remaining = remaining.strip()
    return url, remaining


def build_mission_from_command(
    skill,
    command_name: str,
    context: str,
    notification: dict,
    project_name: str,
    comment_url: Optional[str] = None,
) -> str:
    """Construct a mission string from a GitHub notification command.

    Args:
        skill: The Skill object.
        command_name: The command name (e.g., "rebase").
        context: Additional context text from the @mention.
        notification: The notification dict.
        project_name: The resolved project name.
        comment_url: Optional comment web URL. When set, overrides the
            subject URL and skips context (used by /ask to store only the
            comment URL, keeping missions.md free of raw question text).

    Returns:
        A mission entry string like "- [project:X] /command url context"
    """
    # When a comment URL is explicitly provided (e.g., for /ask), use it
    # directly and skip context — the question text lives on GitHub.
    if comment_url:
        mission_text = f"/{command_name} {comment_url}"
        return f"- [project:{project_name}] {mission_text} 📬"

    # Extract URL from notification subject
    subject_url = notification.get("subject", {}).get("url", "")
    web_url = api_url_to_web_url(subject_url) if subject_url else ""

    # Check if context contains a URL — if so, use that instead
    url_in_context = _extract_url_from_context(context)
    if url_in_context:
        web_url, context = url_in_context

        # Re-resolve project when context URL points to a different repo.
        # Without this, a command like "@bot plan <other-repo-url>" posted
        # on repo A would tag the mission with project A but the URL targets
        # repo B — causing the plan to run in the wrong project directory.
        resolved = _resolve_project_from_url(web_url)
        if resolved:
            project_name = resolved

    # Build mission text
    parts = [f"/{command_name}"]
    if web_url:
        parts.append(web_url)
    if context and skill.github_context_aware:
        parts.append(context)

    mission_text = " ".join(parts)
    # Trailing 📬 marks missions originating from GitHub @mentions.
    # The /list handler repositions it as a leading visual hint.
    return f"- [project:{project_name}] {mission_text} 📬"


def resolve_project_from_notification(notification: dict) -> Optional[Tuple[str, str, str]]:
    """Resolve project name from notification repository.

    Args:
        notification: A notification dict.

    Returns:
        Tuple of (project_name, owner, repo) or None if unknown.
    """
    repo_data = notification.get("repository", {})
    full_name = repo_data.get("full_name", "")
    if not full_name or "/" not in full_name:
        return None

    owner, repo = full_name.split("/", 1)

    from app.utils import project_name_for_path, resolve_project_path

    project_path = resolve_project_path(repo, owner=owner)
    if not project_path:
        return None

    project_name = project_name_for_path(project_path)
    return project_name, owner, repo


def _fetch_and_filter_comment(notification: dict, bot_username: str, max_age_hours: int) -> Optional[dict]:
    """Fetch the triggering comment and check if notification should be skipped.

    Uses latest_comment_url as the fast path, but falls back to searching the
    full thread when the fast path fails (API error, self-mention, or stale URL
    pointing to a comment that doesn't mention the bot).

    Args:
        notification: Notification dict
        bot_username: Bot's GitHub username
        max_age_hours: Maximum age threshold

    Returns:
        The comment dict if notification should be processed, or None to skip.
    """
    thread_id = notification.get("id", "?")
    repo_name = notification.get("repository", {}).get("full_name", "?")

    # Check staleness
    if is_notification_stale(notification, max_age_hours):
        log.debug("GitHub: skipping notification %s from %s — stale (>%dh)", thread_id, repo_name, max_age_hours)
        mark_notification_read(str(notification.get("id", "")))
        return None

    # Fast path: fetch comment from latest_comment_url
    comment = get_comment_from_notification(notification)
    need_thread_search = False

    if not comment:
        # API failure or missing URL — don't give up yet, search the thread
        log.debug("GitHub: notification %s from %s — latest_comment_url failed, will search thread", thread_id, repo_name)
        need_thread_search = True
    elif is_self_mention(comment, bot_username):
        # latest_comment_url points to bot's own comment (race condition)
        log.debug(
            "GitHub: latest comment on %s is self-authored — searching thread for @mention",
            repo_name,
        )
        need_thread_search = True
    elif f"@{bot_username}".lower() not in comment.get("body", "").lower():
        # latest_comment_url shifted to a comment that doesn't mention the bot
        # (e.g., CI bot commented after the @mention, or PR body was returned)
        comment_author = comment.get("user", {}).get("login", "?")
        log.debug(
            "GitHub: latest comment on %s by @%s doesn't mention @%s — searching thread",
            repo_name, comment_author, bot_username,
        )
        need_thread_search = True
    else:
        comment_author = comment.get("user", {}).get("login", "?")
        log.debug("GitHub: notification %s from %s — comment by @%s", thread_id, repo_name, comment_author)

    if need_thread_search:
        mention_comment = find_mention_in_thread(notification, bot_username)
        if mention_comment:
            mention_author = mention_comment.get("user", {}).get("login", "?")
            log.debug(
                "GitHub: found unprocessed @mention by @%s in thread (latest_comment_url was stale)",
                mention_author,
            )
            return mention_comment

        log.debug("GitHub: no unprocessed @mention in thread — skipping notification %s", thread_id)
        mark_notification_read(str(notification.get("id", "")))
        return None

    return comment


def _validate_and_parse_command(
    notification: dict,
    comment: dict,
    config: dict,
    registry: SkillRegistry,
    bot_username: str,
    owner: str,
    repo: str,
) -> Tuple[Optional[object], Optional[str], str]:
    """Validate command and parse from comment.

    Args:
        notification: Notification dict
        comment: Comment dict
        config: Config dict
        registry: Skills registry
        bot_username: Bot's GitHub username
        owner: Repository owner
        repo: Repository name

    Returns:
        Tuple of (skill, command_name, context).
        skill is None if command is invalid or already processed.
        command_name is None if already processed/no valid mention.
    """
    comment_id = str(comment.get("id", ""))
    comment_api_url = comment.get("url", "")

    # Check if already processed
    if check_already_processed(comment_id, bot_username, owner, repo,
                                comment_api_url=comment_api_url):
        log.debug("GitHub: comment %s already processed", comment_id)
        mark_notification_read(str(notification.get("id", "")))
        return None, None, ""

    # Parse command from comment
    nickname = get_github_nickname(config)
    command_result = parse_mention_command(comment.get("body", ""), nickname)
    if not command_result:
        log.debug("GitHub: no valid @mention command in comment %s", comment_id)
        mark_notification_read(str(notification.get("id", "")))
        return None, None, ""

    command_name, context = command_result
    log.debug("GitHub: parsed command=%s context=%s from comment %s", command_name, context, comment_id)

    # Validate command
    skill = validate_command(command_name, registry)
    if not skill:
        log.debug("GitHub: command '%s' is not github-enabled", command_name)
        return None, command_name, context  # Invalid command, but we have the name for error message

    return skill, command_name, context


def _try_nlp_classification(
    comment: dict,
    config: dict,
    projects_config: Optional[dict],
    registry: SkillRegistry,
    bot_username: str,
    project_name: str,
    owner: str,
    repo: str,
) -> Optional[Tuple[object, str, str]]:
    """Attempt NLP intent classification for an unrecognized command.

    Only runs when natural_language is enabled in config. Calls Claude
    to classify the comment text into a known github-enabled command.

    Args:
        comment: Comment dict.
        config: Global config.
        projects_config: Projects config.
        registry: Skills registry.
        bot_username: Bot's GitHub username.
        project_name: Resolved project name.
        owner: Repository owner.
        repo: Repository name.

    Returns:
        Tuple of (skill, command_name, context) if classification succeeded,
        or None if NLP is disabled, failed, or returned no match.
    """
    if not get_github_natural_language(config, project_name, projects_config):
        return None

    # Resolve project path for Claude CLI
    from app.utils import resolve_project_path
    project_path = resolve_project_path(repo, owner=owner)
    if not project_path:
        log.debug("GitHub NLP: could not resolve project path for %s/%s", owner, repo)
        return None

    # Get available commands for the classifier
    commands = get_github_enabled_commands_with_descriptions(registry)
    if not commands:
        return None

    # Extract the full comment text (after @mention, code blocks stripped)
    nickname = get_github_nickname(config)
    from app.github_reply import extract_mention_text
    message = extract_mention_text(comment.get("body", ""), nickname)
    if not message:
        return None

    from app.github_intent import classify_intent

    log.debug("GitHub NLP: classifying intent for: %s", message[:100])
    result = classify_intent(message, commands, project_path)

    if not result or not result.get("command"):
        log.debug("GitHub NLP: no command classified")
        return None

    classified_command = result["command"]
    classified_context = result.get("context", "")

    # Validate the classified command is actually github_enabled
    skill = validate_command(classified_command, registry)
    if not skill:
        log.debug(
            "GitHub NLP: classified command '%s' is not github-enabled",
            classified_command,
        )
        return None

    log.info(
        "GitHub NLP: classified '%s' as /%s for %s/%s",
        message[:80], classified_command, owner, repo,
    )
    return skill, classified_command, classified_context


def _try_reply(
    notification: dict,
    comment: dict,
    config: dict,
    projects_config: Optional[dict],
    bot_username: str,
    owner: str,
    repo: str,
    project_name: str,
    question_text: str,
) -> bool:
    """Attempt to generate and post an AI reply for a non-command @mention.

    Checks reply_enabled config and user permissions before generating.

    Args:
        notification: Notification dict.
        comment: Comment dict.
        config: Global config.
        projects_config: Projects config.
        bot_username: Bot's GitHub username.
        owner: Repository owner.
        repo: Repository name.
        project_name: Resolved project name.
        question_text: The user's question/request text.

    Returns:
        True if reply was generated and posted successfully.
    """
    if not get_github_reply_enabled(config):
        return False

    comment_author = comment.get("user", {}).get("login", "")
    comment_id = str(comment.get("id", ""))

    # Check permissions — same authorized_users as commands
    allowed_users = get_github_authorized_users(config, project_name, projects_config)
    if not check_user_permission(owner, repo, comment_author, allowed_users):
        log.debug(
            "GitHub reply: permission denied for @%s on %s/%s",
            comment_author, owner, repo,
        )
        return False

    # Extract issue number for the thread
    issue_number = extract_issue_number_from_notification(notification)
    if not issue_number:
        log.debug("GitHub reply: could not extract issue number from notification")
        return False

    # Resolve project path for Claude CLI
    from app.utils import resolve_project_path
    project_path = resolve_project_path(repo, owner=owner)
    if not project_path:
        log.debug("GitHub reply: could not resolve project path for %s/%s", owner, repo)
        return False

    log.info(
        "GitHub reply: generating reply for @%s on %s/%s#%s",
        comment_author, owner, repo, issue_number,
    )

    # Notify on Telegram: question received from GitHub
    _notify_github_question(
        comment_author, owner, repo, issue_number, question_text,
    )

    from app.github_reply import (
        fetch_thread_context,
        generate_reply,
        post_reply,
    )

    # Fetch context and generate reply
    thread_context = fetch_thread_context(owner, repo, issue_number)
    reply_text = generate_reply(
        question=question_text,
        thread_context=thread_context,
        owner=owner,
        repo=repo,
        issue_number=issue_number,
        comment_author=comment_author,
        project_path=project_path,
    )

    if not reply_text:
        log.warning("GitHub reply: failed to generate reply for comment %s", comment_id)
        return False

    # Post reply
    if not post_reply(owner, repo, issue_number, reply_text):
        log.warning("GitHub reply: failed to post reply for comment %s", comment_id)
        return False

    # Mark as processed
    comment_api_url = comment.get("url", "")
    add_reaction(owner, repo, comment_id, emoji="eyes",
                 comment_api_url=comment_api_url)
    mark_notification_read(str(notification.get("id", "")))

    # Notify on Telegram: reply posted to GitHub
    _notify_github_reply(
        owner, repo, issue_number, reply_text,
    )

    log.info("GitHub reply: posted reply to @%s on %s/%s#%s", comment_author, owner, repo, issue_number)
    return True


def process_single_notification(
    notification: dict,
    registry: SkillRegistry,
    config: dict,
    projects_config: Optional[dict],
    bot_username: str,
    max_age_hours: int = 24,
) -> Tuple[bool, Optional[str]]:
    """Process a single GitHub notification.

    Full workflow: parse → validate → check permissions → react → create mission.

    Args:
        notification: A notification dict from GitHub API.
        registry: Skills registry.
        config: Global config (from config.yaml).
        projects_config: Projects config (from projects.yaml), or None.
        bot_username: The bot's GitHub username.
        max_age_hours: Max notification age in hours.

    Returns:
        Tuple of (success, error_message). error_message is None on success.
    """
    # Early exit checks + fetch comment (single API call)
    comment = _fetch_and_filter_comment(notification, bot_username, max_age_hours)
    if not comment:
        # No @mention found — try subscription path for subscribed/author notifications
        if _try_subscription_notification(
            notification, config, projects_config, bot_username,
        ):
            mark_notification_read(str(notification.get("id", "")))
            return True, None
        return False, None

    comment_author = comment.get("user", {}).get("login", "")

    # Resolve project
    project_info = resolve_project_from_notification(notification)
    if not project_info:
        repo_name = notification.get("repository", {}).get("full_name", "?")
        log.debug("GitHub: repo %s not found in projects.yaml", repo_name)
        mark_notification_read(str(notification.get("id", "")))
        return False, "Unknown repository — not configured in projects.yaml"

    project_name, owner, repo = project_info
    log.debug("GitHub: resolved project=%s from %s/%s", project_name, owner, repo)

    # Validate and parse command
    skill, command_name, context = _validate_and_parse_command(
        notification, comment, config, registry, bot_username, owner, repo,
    )

    # If command_name is None, already processed or no valid mention
    if command_name is None:
        return False, None

    # Built-in "help" command — reply with available commands list
    if skill is None and command_name == "help":
        _handle_help_command(
            notification, comment, registry, bot_username, owner, repo,
        )
        return False, None

    # If skill is None but we have a command_name, it's an invalid command
    if skill is None:
        nlp_enabled = get_github_natural_language(
            config, project_name, projects_config,
        )

        if nlp_enabled:
            # Route to /gh_request — let it classify and dispatch properly.
            # This replaces direct NLP→command mapping which broke when the
            # classified command's args didn't match (e.g. /fix without issue URL).
            gh_request_skill = validate_command("gh_request", registry)
            if gh_request_skill:
                nickname = get_github_nickname(config)
                from app.github_reply import extract_mention_text
                full_text = extract_mention_text(comment.get("body", ""), nickname)
                if full_text:
                    skill = gh_request_skill
                    command_name = "gh_request"
                    context = full_text
                    log.info(
                        "GitHub NLP: routing to /gh_request for %s/%s: %s",
                        owner, repo, full_text[:80],
                    )
        else:
            # Try NLP intent classification (legacy path for non-NLP projects)
            nlp_result = _try_nlp_classification(
                comment, config, projects_config, registry,
                bot_username, project_name, owner, repo,
            )
            if nlp_result:
                nlp_skill, nlp_command, nlp_context = nlp_result
                skill = nlp_skill
                command_name = nlp_command
                context = nlp_context

    # If still no skill after NLP, fall through to reply/error
    if skill is None and command_name is not None and command_name != "help":
        # Try AI reply before falling back to error message
        full_question = f"{command_name} {context}".strip()
        if _try_reply(
            notification, comment, config, projects_config,
            bot_username, owner, repo, project_name, full_question,
        ):
            return False, None  # Reply posted instead of error
        mark_notification_read(str(notification.get("id", "")))
        help_msg = format_help_message(command_name, registry, bot_username)
        return False, help_msg

    # Check permissions
    allowed_users = get_github_authorized_users(config, project_name, projects_config)
    if not check_user_permission(owner, repo, comment_author, allowed_users):
        log.debug(
            "GitHub: permission denied for @%s on %s/%s (allowed: %s)",
            comment_author, owner, repo,
            ", ".join(allowed_users) if allowed_users else "none",
        )
        mark_notification_read(str(notification.get("id", "")))
        return False, "Permission denied. Only users with write access can trigger bot commands."

    # Scan context text for prompt injection (free-form text is the attack vector)
    if context and context.strip():
        from app.prompt_guard import scan_mission_text
        from app.config import get_prompt_guard_config

        guard_config = get_prompt_guard_config()
        if guard_config["enabled"]:
            guard_result = scan_mission_text(context)
            if guard_result.blocked:
                log.warning(
                    "GitHub: prompt guard flagged @%s context: %s | %s",
                    comment_author, guard_result.reason, context[:100],
                )
                _quarantine_github_mission(
                    context, guard_result.reason, comment_author,
                )
                if guard_config["block_mode"]:
                    mark_notification_read(str(notification.get("id", "")))
                    return False, f"Mission blocked by prompt guard: {guard_result.reason}"

    # Build and insert mission BEFORE reacting (so crash doesn't lose command)
    # For /ask: pass the comment's web URL so the mission stores only the URL,
    # not the raw question text (which may contain chars that corrupt missions.md).
    ask_comment_url = None
    if command_name == "ask":
        ask_comment_url = comment.get("html_url") or None
    mission_entry = build_mission_from_command(
        skill, command_name, context, notification, project_name,
        comment_url=ask_comment_url,
    )
    log.info("GitHub: inserting mission from @%s: %s", comment_author, mission_entry)

    from app.utils import insert_pending_mission
    from pathlib import Path
    import os

    koan_root = os.environ.get("KOAN_ROOT", "")
    if not koan_root:
        log.error("GitHub: KOAN_ROOT not set — cannot insert mission")
        mark_notification_read(str(notification.get("id", "")))
        return False, "KOAN_ROOT not configured"
    missions_path = Path(koan_root) / "instance" / "missions.md"
    try:
        insert_pending_mission(missions_path, mission_entry)
    except OSError as e:
        log.warning("GitHub: failed to insert mission: %s", e)
        # Mark notification as read to prevent infinite re-processing
        mark_notification_read(str(notification.get("id", "")))
        return False, f"Failed to queue mission: {e}"

    # React AFTER mission is persisted (marks as processed)
    comment_id = str(comment.get("id", ""))
    comment_api_url = comment.get("url", "")
    add_reaction(owner, repo, comment_id, comment_api_url=comment_api_url)

    # Mark notification as read
    mark_notification_read(str(notification.get("id", "")))

    log.info("GitHub: created mission from @%s: %s", comment_author, command_name)
    return True, None


def post_error_reply(
    owner: str,
    repo: str,
    issue_number: str,
    comment_id: str,
    error_message: str,
    comment_api_url: str = "",
) -> bool:
    """Post an error reply to a GitHub comment.

    Includes deduplication — won't post the same error twice for the same comment.

    Args:
        owner: Repository owner.
        repo: Repository name.
        issue_number: Issue or PR number.
        comment_id: The triggering comment ID.
        error_message: The error message to post.
        comment_api_url: The comment's canonical API URL for correct
            reactions endpoint (handles PR review comments, etc.).

    Returns:
        True if posted successfully.
    """
    # Deduplication key
    error_key = f"{comment_id}:{error_message}"
    if error_key in _error_replies:
        return False

    from app.github import api

    body = f"❌ {error_message}"
    try:
        api(
            f"repos/{owner}/{repo}/issues/{issue_number}/comments",
            method="POST",
            extra_args=["-f", f"body={body}"],
        )

        # Add reaction to mark as processed — only suppress future
        # retries if the reaction was actually placed.
        reacted = add_reaction(owner, repo, comment_id,
                               comment_api_url=comment_api_url)
        if reacted:
            _error_replies.add(error_key)
        return True
    except RuntimeError:
        return False


def _fetch_new_comments_since(
    owner: str,
    repo: str,
    issue_number: str,
    since_comment_id: Optional[int],
    bot_username: str,
) -> List[dict]:
    """Fetch comments on a thread that are newer than since_comment_id.

    Filters out comments from the bot itself to avoid self-reply loops.

    Returns:
        List of comment dicts from other users, newest last.
    """
    import json as _json

    from app.github import api as gh_api

    try:
        raw = gh_api(
            f"repos/{owner}/{repo}/issues/{issue_number}/comments",
            jq='[.[] | {id: .id, body: .body, user_login: .user.login}]',
        )
        comments = _json.loads(raw) if raw else []
    except (RuntimeError, ValueError):
        return []

    if not isinstance(comments, list):
        return []

    # Filter: only comments after since_comment_id, not from the bot
    result = []
    for c in comments:
        cid = c.get("id", 0)
        author = c.get("user_login", "")
        if author.lower() == bot_username.lower():
            continue
        if since_comment_id is not None and cid <= since_comment_id:
            continue
        result.append(c)

    return result


def _try_subscription_notification(
    notification: dict,
    config: dict,
    projects_config: Optional[dict],
    bot_username: str,
) -> bool:
    """Handle a subscription/author notification by queuing a /reply mission.

    Called when:
    - subscribe_enabled is True
    - notification reason is 'subscribed' or 'author'
    - no @mention was found (standard command path returned None)

    Returns True if a /reply mission was queued.
    """
    import os
    from pathlib import Path

    reason = notification.get("reason", "")
    if reason not in ("subscribed", "author"):
        return False

    if not get_github_subscribe_enabled(config):
        return False

    # Resolve project
    project_info = resolve_project_from_notification(notification)
    if not project_info:
        return False

    project_name, owner, repo = project_info
    issue_number = extract_issue_number_from_notification(notification)
    if not issue_number:
        return False

    koan_root = os.environ.get("KOAN_ROOT", "")
    if not koan_root:
        return False
    instance_dir = Path(koan_root) / "instance"

    from app.thread_subscriptions import (
        get_last_replied_comment_id,
        has_pending_mission,
        make_thread_key,
        set_pending_mission,
    )

    thread_key = make_thread_key(owner, repo, issue_number)

    # Already have a pending mission for this thread
    if has_pending_mission(instance_dir, thread_key):
        log.debug("GitHub subscribe: pending mission exists for %s", thread_key)
        return False

    # Check for new comments since our last reply
    last_id = get_last_replied_comment_id(instance_dir, thread_key)
    new_comments = _fetch_new_comments_since(
        owner, repo, issue_number, last_id, bot_username,
    )
    if not new_comments:
        log.debug("GitHub subscribe: no new comments on %s", thread_key)
        return False

    # Build web URL for the thread
    subject_url = notification.get("subject", {}).get("url", "")
    web_url = api_url_to_web_url(subject_url) if subject_url else ""
    if not web_url:
        web_url = f"https://github.com/{owner}/{repo}/issues/{issue_number}"

    # Queue /reply mission
    mission_entry = f"- [project:{project_name}] /reply {web_url}"
    log.info("GitHub subscribe: queuing reply mission for %s", thread_key)

    from app.utils import insert_pending_mission

    missions_path = Path(koan_root) / "instance" / "missions.md"
    try:
        insert_pending_mission(missions_path, mission_entry)
    except OSError as e:
        log.warning("GitHub subscribe: failed to insert mission: %s", e)
        return False

    # Mark as pending to prevent duplicate missions
    set_pending_mission(instance_dir, thread_key, True)
    return True


def _notify_github_question(
    author: str, owner: str, repo: str, issue_number: str, question: str,
) -> None:
    """Send ❓ Telegram notification when a question is received from GitHub."""
    try:
        from app.notify import send_telegram, NotificationPriority
        # Truncate question for Telegram readability
        short = question[:200] + "…" if len(question) > 200 else question
        send_telegram(
            f"❓ GitHub question from @{author}\n"
            f"{owner}/{repo}#{issue_number}: {short}",
            priority=NotificationPriority.ACTION,
        )
    except Exception as e:
        log.warning("Failed to send GitHub question notification: %s", e)


def _notify_github_reply(
    owner: str, repo: str, issue_number: str, reply_text: str,
) -> None:
    """Send 💬 Telegram notification when Kōan posts a reply on GitHub."""
    try:
        from app.notify import send_telegram, NotificationPriority
        short = reply_text[:200] + "…" if len(reply_text) > 200 else reply_text
        send_telegram(
            f"💬 Replied on GitHub\n"
            f"{owner}/{repo}#{issue_number}: {short}",
            priority=NotificationPriority.ACTION,
        )
    except Exception as e:
        log.warning("Failed to send GitHub reply notification: %s", e)


def extract_issue_number_from_notification(notification: dict) -> Optional[str]:
    """Extract issue/PR number from a notification.

    Works for both issues and pull requests.
    """
    subject_url = notification.get("subject", {}).get("url", "")
    if not subject_url:
        return None

    # API URL: .../issues/42 or .../pulls/42
    match = re.search(r'/(?:issues|pulls)/(\d+)', subject_url)
    return match.group(1) if match else None
