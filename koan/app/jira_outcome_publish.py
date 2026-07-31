"""Publish end-of-mission Jira status for Jira-linked missions."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Dict, Optional, Tuple

from app.github_url_parser import search_jira_url
from app.jira_notifications import (
    jira_add_comment,
    jira_edit_comment,
    jira_list_comments_checked,
)
from app.run_log import log_safe as _log_runner
from app.tracker_comment_format import build_pr_comment_failure, build_pr_comment_success

_PR_URL_RE = re.compile(r"https?://[^/]*github[^\s)]+/pull/\d+")
_MARKER_PREFIX = "<!-- koan-jira-outcome:"


def _fetch_pr_details(pr_url: str) -> Tuple[str, str]:
    """Best-effort fetch of a PR's title and body via the ``gh`` CLI.

    Returns ``("", "")`` on any error so the caller falls back to a
    link-only comment rather than failing the outcome publish.
    """
    if not pr_url:
        return "", ""
    try:
        from app.github import run_gh

        raw = run_gh("pr", "view", pr_url, "--json", "title,body")
        data = json.loads(raw) if raw else {}
        if isinstance(data, dict):
            return str(data.get("title") or ""), str(data.get("body") or "")
    except Exception as e:  # network/auth/parse — degrade gracefully
        _log_runner("jira", f"Could not fetch PR details for {pr_url}: {e}")
    return "", ""


def extract_pr_url(text: str) -> str:
    """Extract the first GitHub PR URL from arbitrary mission output text."""
    if not text:
        return ""
    match = _PR_URL_RE.search(text)
    return match.group(0) if match else ""


def _extract_command_name(mission_title: str) -> str:
    match = re.search(r"^\s*/([a-zA-Z0-9_]+)\b", mission_title or "")
    return (match.group(1).lower() if match else "mission")


def _extract_failure_reason(content: str, exit_code: int) -> str:
    for raw in (content or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        lowered = line.lower()
        if lowered.startswith(("# mission:", "project:", "started:", "run:", "mode:")):
            continue
        if lowered in {"---"}:
            continue
        if lowered.startswith("[cli]"):
            continue
        return line[:220]
    return f"Mission failed (exit code {exit_code})."


def _marker_for(issue_key: str, command_name: str) -> str:
    token = f"{issue_key}:{command_name}"
    digest = hashlib.sha1(token.encode("utf-8")).hexdigest()[:12]
    return f"{_MARKER_PREFIX}{digest} -->"


def _build_status_comment(
    issue_key: str,
    command_name: str,
    body_text: str,
) -> str:
    marker = _marker_for(issue_key, command_name)
    return f"{body_text}\n\n{marker}".strip()


def _upsert_status_comment(
    issue_key: str,
    command_name: str,
    body_text: str,
) -> Tuple[bool, str]:
    marker = _marker_for(issue_key, command_name)
    status_body = _build_status_comment(issue_key, command_name, body_text)
    try:
        comments = jira_list_comments_checked(issue_key)
    except Exception as e:
        # A failed lookup is indistinguishable from "no status comment yet".
        # Creating on that signal is how one outcome becomes a pile of them.
        _log_runner("jira", f"Comment lookup failed for {issue_key}: {e}")
        return False, "lookup_failed"
    existing = next((c for c in comments if marker in (c.get("body") or "")), None)

    if existing:
        ok = jira_edit_comment(issue_key, existing.get("id", ""), status_body)
        return ok, "updated" if ok else "update_failed"

    ok = jira_add_comment(issue_key, status_body)
    return ok, "created" if ok else "create_failed"


def upsert_jira_comment(
    issue_key: str,
    command_name: str,
    body_text: str,
) -> Tuple[bool, str]:
    """Idempotently post or update a marker-tagged Jira status comment.

    Shared entry point so every Jira commenter (end-of-mission publisher,
    draft-PR submission helper) dedups under the same ``(issue_key,
    command_name)`` marker instead of stacking duplicate comments.
    """
    return _upsert_status_comment(issue_key, command_name, body_text)


def publish_jira_mission_outcome(
    mission_title: str,
    pending_content: str,
    exit_code: int,
    base_branch: Optional[str] = None,
) -> Dict[str, str]:
    """Publish final Jira status for Jira-linked missions.

    Behavior:
    - If mission has no Jira URL: no-op.
    - On success with PR URL found: publish PR status (create or update).
    - On failure (non-zero exit): publish failure status (create or update).
    """
    match = search_jira_url(mission_title or "")
    if not match:
        return {"published": "false", "reason": "no_jira_url"}
    issue_url, issue_key = match

    command_name = _extract_command_name(mission_title)
    pr_url = extract_pr_url(pending_content)

    if exit_code == 0 and not pr_url:
        _log_runner(
            "jira",
            f"Outcome publish skipped for {issue_key}: success without PR URL",
        )
        return {"published": "false", "reason": "success_without_pr"}

    if pr_url:
        pr_title, pr_body = _fetch_pr_details(pr_url)
        body = build_pr_comment_success(
            "jira",
            pr_url=pr_url,
            pr_title=pr_title,
            pr_body=pr_body,
            skill_name=command_name,
            base_branch=base_branch,
        )
        ok, mode = _upsert_status_comment(issue_key, command_name, body)
        _log_runner(
            "jira",
            f"Outcome publish for {issue_key}: mode={mode} outcome=pr_success pr={pr_url}",
        )
        return {
            "published": "true" if ok else "false",
            "reason": mode,
            "issue_url": issue_url,
            "issue_key": issue_key,
            "pr_url": pr_url,
            "outcome": "pr_success",
        }

    reason = _extract_failure_reason(pending_content, exit_code)
    body = build_pr_comment_failure(
        "jira",
        reason=reason,
        branch="",
        base_branch=base_branch,
        skill_name=command_name,
    )
    ok, mode = _upsert_status_comment(issue_key, command_name, body)
    _log_runner(
        "jira",
        f"Outcome publish for {issue_key}: mode={mode} outcome=failure reason={reason[:120]}",
    )
    return {
        "published": "true" if ok else "false",
        "reason": mode,
        "issue_url": issue_url,
        "issue_key": issue_key,
        "outcome": "failure",
    }
