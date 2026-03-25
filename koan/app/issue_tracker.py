"""Unified issue tracker client for PR review enrichment.

Provides parsing and fetching helpers for JIRA tickets and GitHub cross-repo
issues referenced in PR descriptions. Fetched context is injected into the
review prompt as {ISSUE_CONTEXT}.

Functions:
    parse_jira_ticket_ids(text)     — extract PROJ-123 ticket IDs
    parse_github_issue_refs(text)   — extract owner/repo#123 cross-repo refs
    fetch_jira_issues(ids, config)  — fetch JIRA ticket summaries via REST API
    fetch_github_issues(refs, cfg)  — fetch GitHub issue summaries via gh CLI
    fetch_issue_context(body, cfg)  — top-level dispatcher; returns enriched block

Design decisions:
    - Uses urllib.request for JIRA HTTP calls — no httpx/requests dependency.
    - Uses subprocess to call gh CLI for GitHub issues — no Python GitHub SDK.
    - Enforces 5s timeout per JIRA ticket fetch.
    - Caps each ticket/issue description excerpt at 500 chars.
    - Caps total output at 1000 chars to limit prompt token usage.
    - Returns "" on any failure — never propagates exceptions to callers.
"""

import base64
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from typing import List, Optional, Tuple


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

# Matches standard JIRA ticket IDs: 2-10 uppercase letters, dash, digits
_JIRA_RE = re.compile(r'\b([A-Z]{2,10}-\d+)\b')

# Matches cross-repo GitHub issue refs: owner/repo#number
# Intentionally excludes in-repo #123 refs (ambiguous without knowing current repo)
_GITHUB_ISSUE_RE = re.compile(
    r'\b([a-zA-Z0-9](?:[a-zA-Z0-9._-]*[a-zA-Z0-9])?)'  # owner
    r'/'
    r'([a-zA-Z0-9](?:[a-zA-Z0-9._-]*[a-zA-Z0-9])?)'    # repo
    r'#(\d+)\b'                                           # issue number
)

_MAX_EXCERPT_CHARS = 500
_MAX_TOTAL_CHARS = 1000


def parse_jira_ticket_ids(text: str) -> List[str]:
    """Extract JIRA ticket IDs from text.

    Returns a deduplicated list of ticket IDs in order of first appearance.
    Matches patterns like PROJ-42, ABC-7, FEATURE-999.

    Args:
        text: Text to scan (PR title, body, etc.)

    Returns:
        List of ticket ID strings, e.g. ["PROJ-42", "ABC-7"].
    """
    seen: set = set()
    result: List[str] = []
    for m in _JIRA_RE.finditer(text or ""):
        tid = m.group(1)
        if tid not in seen:
            seen.add(tid)
            result.append(tid)
    return result


def parse_github_issue_refs(text: str) -> List[Tuple[str, str, int]]:
    """Extract cross-repo GitHub issue references from text.

    Matches patterns like myorg/myrepo#99. In-repo #123 refs are intentionally
    excluded — they're ambiguous without knowing the current repository.

    Args:
        text: Text to scan (PR body, description, etc.)

    Returns:
        Deduplicated list of (owner, repo, number) tuples in order of
        first appearance. E.g. [("myorg", "myrepo", 99)].
    """
    seen: set = set()
    result: List[Tuple[str, str, int]] = []
    for m in _GITHUB_ISSUE_RE.finditer(text or ""):
        owner, repo, num_str = m.group(1), m.group(2), m.group(3)
        key = (owner.lower(), repo.lower(), int(num_str))
        if key not in seen:
            seen.add(key)
            result.append((owner, repo, int(num_str)))
    return result


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

def fetch_jira_issues(ticket_ids: List[str], config: dict) -> str:
    """Fetch JIRA ticket summaries via the REST API.

    Uses HTTP Basic auth (email:api_token). Fetches each ticket individually
    via GET /rest/api/3/issue/{id}. Returns a formatted markdown block, or ""
    on any failure.

    Args:
        ticket_ids: List of JIRA ticket IDs to fetch.
        config: JIRA config dict with keys: base_url, email, api_token.

    Returns:
        Formatted markdown block capped at _MAX_TOTAL_CHARS, or "".
    """
    if not ticket_ids:
        return ""

    base_url = config.get("base_url", "").rstrip("/")
    email = config.get("email", "")
    api_token = config.get("api_token", "")

    if not base_url or not email or not api_token:
        return ""

    credentials = base64.b64encode(f"{email}:{api_token}".encode()).decode()
    auth_header = f"Basic {credentials}"

    lines: List[str] = ["## Issue Tracker Context"]
    total = len(lines[0]) + 1

    for tid in ticket_ids:
        url = f"{base_url}/rest/api/3/issue/{tid}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": auth_header,
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                print(
                    f"[issue_tracker] JIRA {tid}: HTTP {e.code} (skipping)",
                    file=sys.stderr,
                )
            else:
                print(
                    f"[issue_tracker] JIRA {tid}: HTTP error {e.code} (skipping)",
                    file=sys.stderr,
                )
            continue
        except Exception as e:
            print(
                f"[issue_tracker] JIRA {tid}: fetch failed ({e}), skipping",
                file=sys.stderr,
            )
            continue

        fields = data.get("fields", {}) or {}
        summary = (fields.get("summary") or "").strip()
        description_raw = fields.get("description") or ""
        description = _extract_jira_description(description_raw)
        if len(description) > _MAX_EXCERPT_CHARS:
            description = description[:_MAX_EXCERPT_CHARS] + "..."

        entry = f"- {tid}: {summary}"
        if description:
            entry += f"\n  > {description}"
        entry_len = len(entry) + 1  # +1 for newline

        if total + entry_len > _MAX_TOTAL_CHARS:
            break
        lines.append(entry)
        total += entry_len

    if len(lines) <= 1:
        # No tickets fetched — return empty (header-only is useless)
        return ""

    return "\n".join(lines)


def _extract_jira_description(description) -> str:
    """Extract plain text from a JIRA description field.

    JIRA API v3 returns description as Atlassian Document Format (ADF) JSON.
    This function extracts readable text from ADF or falls back gracefully
    to stringify non-dict values (plain text from older API versions).
    """
    if not description:
        return ""
    if isinstance(description, str):
        # Plain text or legacy API response
        return description.strip()
    if not isinstance(description, dict):
        return str(description).strip()

    # ADF format: recursively extract text nodes
    return _adf_to_text(description).strip()


def _adf_to_text(node: dict, depth: int = 0) -> str:
    """Recursively extract text from an ADF document node."""
    if not isinstance(node, dict):
        return ""
    node_type = node.get("type", "")

    # Leaf text node
    if node_type == "text":
        return node.get("text", "")

    # Block nodes that add a newline separator
    content = node.get("content") or []
    parts = [_adf_to_text(child, depth + 1) for child in content]
    text = " ".join(p for p in parts if p)

    if node_type in ("paragraph", "heading", "bulletList", "listItem",
                     "orderedList", "blockquote", "codeBlock"):
        return text + "\n"

    return text


def fetch_github_issues(refs: List[Tuple[str, str, int]], config: dict) -> str:
    """Fetch GitHub cross-repo issue summaries via the gh CLI.

    Calls ``gh issue view owner/repo#N --json title,body`` for each ref.
    Returns a formatted markdown block, or "" on any failure.

    Args:
        refs: List of (owner, repo, number) tuples.
        config: GitHub config dict (unused beyond type check; gh CLI handles auth).

    Returns:
        Formatted markdown block capped at _MAX_TOTAL_CHARS, or "".
    """
    if not refs:
        return ""

    lines: List[str] = ["## Issue Tracker Context"]
    total = len(lines[0]) + 1

    for owner, repo, number in refs:
        issue_ref = f"{owner}/{repo}#{number}"
        try:
            result = subprocess.run(
                ["gh", "issue", "view", issue_ref, "--json", "title,body"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except FileNotFoundError:
            print(
                "[issue_tracker] gh CLI not found — GitHub issue fetching unavailable",
                file=sys.stderr,
            )
            break
        except subprocess.TimeoutExpired:
            print(
                f"[issue_tracker] gh timed out for {issue_ref}, skipping",
                file=sys.stderr,
            )
            continue
        except Exception as e:
            print(
                f"[issue_tracker] gh failed for {issue_ref}: {e}",
                file=sys.stderr,
            )
            continue

        if result.returncode != 0:
            print(
                f"[issue_tracker] gh non-zero exit for {issue_ref} "
                f"(exit={result.returncode}), skipping",
                file=sys.stderr,
            )
            continue

        try:
            data = json.loads(result.stdout)
        except (json.JSONDecodeError, ValueError):
            print(
                f"[issue_tracker] gh returned non-JSON for {issue_ref}, skipping",
                file=sys.stderr,
            )
            continue

        title = (data.get("title") or "").strip()
        body = (data.get("body") or "").strip()
        if len(body) > _MAX_EXCERPT_CHARS:
            body = body[:_MAX_EXCERPT_CHARS] + "..."

        entry = f"- {issue_ref}: {title}"
        if body:
            entry += f"\n  > {body}"
        entry_len = len(entry) + 1

        if total + entry_len > _MAX_TOTAL_CHARS:
            break
        lines.append(entry)
        total += entry_len

    if len(lines) <= 1:
        return ""

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Top-level dispatcher
# ---------------------------------------------------------------------------

def fetch_issue_context(pr_body: str, config: dict) -> str:
    """Fetch issue context for PR review enrichment.

    Parses the PR body for issue references, fetches summaries from the
    appropriate backend, and returns a formatted block capped at 1000 chars.

    Args:
        pr_body: PR description text to scan for issue references.
        config: Issue tracker config dict from get_issue_tracker_config().
                Must have at least {"type": "jira"|"github"}.

    Returns:
        Formatted markdown block (e.g. "## Issue Tracker Context\\n- PROJ-42: ..."),
        or "" when no references are found or all fetches fail.
    """
    if not config:
        return ""

    tracker_type = config.get("type", "").lower()
    body = pr_body or ""

    if tracker_type == "jira":
        ticket_ids = parse_jira_ticket_ids(body)
        if not ticket_ids:
            return ""
        return fetch_jira_issues(ticket_ids, config)

    if tracker_type == "github":
        refs = parse_github_issue_refs(body)
        if not refs:
            return ""
        return fetch_github_issues(refs, config)

    return ""
