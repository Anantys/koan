"""Kōan /babysit skill — monitor open PRs and queue fix missions.

Commands:
    /babysit            — show status of all monitored PRs
    /babysit on         — enable PR babysitting
    /babysit off        — disable PR babysitting
    /babysit <pr-url>   — force-check a specific PR right now
"""

import re

_PR_URL_RE = re.compile(
    r"https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)"
)


def handle(ctx):
    """Handle /babysit command."""
    args = (ctx.args or "").strip().lower()

    if args == "on":
        return _toggle(ctx, enabled=True)

    if args == "off":
        return _toggle(ctx, enabled=False)

    # Check for a PR URL (force-check)
    pr_match = _PR_URL_RE.search(ctx.args or "")
    if pr_match:
        return _force_check(ctx, pr_match)

    # Default: show status
    return _show_status(ctx)


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------

def _show_status(ctx):
    """Display current babysit status and tracked PRs."""
    try:
        from app.pr_babysit import _babysit_enabled, get_babysit_status
        from app.utils import load_config
    except ImportError as e:
        return f"❌ Babysit module unavailable: {e}"

    enabled = _babysit_enabled()
    status_flag = "✅ enabled" if enabled else "⏸ disabled"

    tracked = get_babysit_status(str(ctx.instance_dir))
    if not tracked:
        return (
            f"🔍 PR babysitting: {status_flag}\n"
            "No PRs currently tracked.\n"
            "Use `/babysit on` to enable automated PR monitoring."
        )

    lines = [f"🔍 PR babysitting: {status_flag}", f"{len(tracked)} PR(s) tracked:\n"]
    for entry in tracked[:10]:  # cap display at 10
        url = entry.get("url", "?")
        ci = entry.get("last_ci_status") or "—"
        review = entry.get("last_review_decision") or "—"
        action = entry.get("last_action") or "none"
        checked = entry.get("last_checked_at", "never")[:16] if entry.get("last_checked_at") else "never"
        lines.append(
            f"• {url}\n"
            f"  CI: {ci} | Review: {review} | Last action: {action} | Checked: {checked}"
        )

    if len(tracked) > 10:
        lines.append(f"… and {len(tracked) - 10} more")

    return "\n".join(lines)


def _toggle(ctx, enabled: bool):
    """Enable or disable babysitting via config.yaml."""
    try:
        from app.utils import atomic_write
        import yaml
    except ImportError as e:
        return f"❌ Cannot toggle babysit: {e}"

    config_path = ctx.instance_dir / "config.yaml"
    if not config_path.exists():
        return "❌ config.yaml not found in instance directory."

    try:
        content = config_path.read_text()
        config = yaml.safe_load(content) or {}
    except Exception as e:
        return f"❌ Could not read config.yaml: {e}"

    # Update the babysit section
    babysit = config.get("pr_babysit") or {}
    babysit["enabled"] = enabled
    config["pr_babysit"] = babysit

    try:
        atomic_write(config_path, yaml.dump(config, default_flow_style=False, allow_unicode=True))
    except Exception as e:
        return f"❌ Could not update config.yaml: {e}"

    state = "enabled ✅" if enabled else "disabled ⏸"
    return f"🔍 PR babysitting {state}."


def _force_check(ctx, pr_match):
    """Force-check a specific PR right now."""
    owner = pr_match.group("owner")
    repo = pr_match.group("repo")
    number = pr_match.group("number")
    pr_url = f"https://github.com/{owner}/{repo}/pull/{number}"

    try:
        from app.pr_babysit import (
            _get_babysit_config,
            _get_tracker_entry,
            check_pr_health,
            queue_fix_missions,
            _extract_ci_status,
            _run_gh,
        )
        import json
    except ImportError as e:
        return f"❌ Babysit module unavailable: {e}"

    # Fetch PR info via gh
    raw = _run_gh(
        "pr", "view", str(number),
        "--repo", f"{owner}/{repo}",
        "--json",
        "number,title,headRefName,url,updatedAt,reviewDecision,"
        "mergeStateStatus,statusCheckRollup,comments,isDraft",
        timeout=30,
    )
    if not raw:
        return f"❌ Could not fetch PR #{number} from {owner}/{repo}."

    try:
        pr_data = json.loads(raw)
    except json.JSONDecodeError:
        return "❌ Could not parse PR data."

    pr = {
        "url": pr_url,
        "number": int(number),
        "title": pr_data.get("title", ""),
        "headRefName": pr_data.get("headRefName", ""),
        "owner": owner,
        "repo": repo,
        "updatedAt": pr_data.get("updatedAt", ""),
        "reviewDecision": pr_data.get("reviewDecision"),
        "mergeStateStatus": pr_data.get("mergeStateStatus"),
        "isDraft": pr_data.get("isDraft", False),
        "statusCheckRollup": _extract_ci_status(pr_data.get("statusCheckRollup")),
        "commentCount": len(pr_data.get("comments", [])),
    }

    cfg = _get_babysit_config()
    tracker_entry = _get_tracker_entry(str(ctx.instance_dir), pr_url)
    actions = check_pr_health(pr, tracker_entry, cfg)

    if not actions:
        return f"✅ PR #{number} looks healthy — no action needed."

    missions_path = ctx.instance_dir / "missions.md"
    queued = queue_fix_missions(
        pr, actions, missions_path, ctx.instance_dir, cfg, notify_on_fix=False,
    )

    if queued:
        action_list = "\n".join(f"  • {q}" for q in queued)
        return f"🔍 Babysit force-check for PR #{number}:\n{action_list}"
    return f"⏸ PR #{number} has issues but is in cooldown — no new missions queued."
