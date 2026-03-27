"""Attention zone aggregator for the Kōan dashboard.

Aggregates items requiring human action from multiple sources:
- Failed missions
- PRs with failing CI
- PRs awaiting review
- Stale PRs (open > 7 days)
- Quota pause signal
- GitHub @mention notifications (gated by config flag)

Each item has: id, severity, source, title, detail, url, age_seconds, created_at.
Severities: critical > warning > info.
Dismissed items are tracked in instance/.koan-attention-dismissed.json.
"""

import fcntl
import hashlib
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.signals import PAUSE_FILE, QUOTA_RESET_FILE

# Stale PR threshold in seconds (7 days)
_STALE_PR_SECONDS = 7 * 24 * 3600

# Severity ordering for sorting
_SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}

# Dismissed items file name
_DISMISSED_FILE = ".koan-attention-dismissed.json"

# Attention items cache (TTL: 30 seconds)
_attention_cache: Optional[tuple] = None  # (items, timestamp)
_ATTENTION_CACHE_TTL = 30

# Log GitHub auth warnings once per process
_github_auth_warned = False


# ---------------------------------------------------------------------------
# ID helpers
# ---------------------------------------------------------------------------

def _make_id(*parts: str) -> str:
    """Return a short deterministic ID from the given parts."""
    raw = ":".join(parts)
    return hashlib.md5(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Dismissed-items persistence
# ---------------------------------------------------------------------------

def _dismissed_file_path(koan_root: str) -> Path:
    return Path(koan_root) / "instance" / _DISMISSED_FILE


def load_dismissed(koan_root: str) -> set:
    """Load the set of dismissed item IDs from disk."""
    path = _dismissed_file_path(koan_root)
    if not path.exists():
        return set()
    try:
        with open(path) as f:
            data = json.load(f)
        return set(data) if isinstance(data, list) else set()
    except (OSError, json.JSONDecodeError):
        return set()


def save_dismissed(koan_root: str, dismissed: set) -> None:
    """Atomically persist the set of dismissed item IDs."""
    path = _dismissed_file_path(koan_root)
    try:
        data = sorted(dismissed)
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=str(path.parent),
            delete=False,
            suffix=".tmp",
        ) as tmp:
            fcntl.flock(tmp, fcntl.LOCK_EX)
            json.dump(data, tmp)
            tmp_path = tmp.name
        os.replace(tmp_path, path)
    except OSError:
        pass


def dismiss_item(koan_root: str, item_id: str) -> None:
    """Add *item_id* to the dismissed set."""
    dismissed = load_dismissed(koan_root)
    dismissed.add(item_id)
    save_dismissed(koan_root, dismissed)


# ---------------------------------------------------------------------------
# Source aggregators
# ---------------------------------------------------------------------------

def _age_seconds(iso_ts: str) -> int:
    """Return seconds since an ISO-8601 timestamp (UTC)."""
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return max(0, int(time.time() - dt.timestamp()))
    except (ValueError, AttributeError):
        return 0


def _collect_failed_missions(koan_root: str) -> list:
    """Return attention items for failed missions."""
    items = []
    try:
        from app.missions import parse_sections
        from app.utils import KOAN_ROOT as _

        missions_file = Path(koan_root) / "instance" / "missions.md"
        if not missions_file.exists():
            return []
        content = missions_file.read_text()
        sections = parse_sections(content)
        failed = sections.get("failed", [])
        for mission_text in failed:
            text_hash = hashlib.md5(mission_text.encode()).hexdigest()[:8]
            item_id = _make_id("failed-mission", text_hash)
            # Strip leading "- " and project tags for display
            display = mission_text.strip().lstrip("- ")
            import re
            display = re.sub(r"\[projec?t:[a-zA-Z0-9_-]+\]\s*", "", display).strip()
            items.append({
                "id": item_id,
                "severity": "critical",
                "source": "mission",
                "title": "Failed mission",
                "detail": display[:120] + ("…" if len(display) > 120 else ""),
                "url": "/missions",
                "age_seconds": 0,
                "created_at": "",
            })
    except Exception as e:
        print(f"[attention] error collecting failed missions: {e}", file=sys.stderr)
    return items


def _collect_pr_items(koan_root: str, project_filter: str = "") -> list:
    """Return attention items from PR data (failing CI, review required, stale)."""
    items = []
    try:
        from app.pr_tracker import fetch_all_prs
        data = fetch_all_prs(koan_root, project_filter=project_filter, author_only=True)
        prs = data.get("prs", [])
        for pr in prs:
            if pr.get("isDraft"):
                continue
            pr_number = pr.get("number", 0)
            project = pr.get("project", "")
            title = pr.get("title", f"PR #{pr_number}")
            url = pr.get("url", "")
            created_at = pr.get("createdAt", "")
            age = _age_seconds(created_at)
            pr_id_base = f"pr:{project}:{pr_number}"

            # Failing CI — check statusCheckRollup
            rollup = pr.get("statusCheckRollup") or []
            if isinstance(rollup, list):
                failing = any(
                    c.get("conclusion") in ("FAILURE", "ERROR", "CANCELLED")
                    for c in rollup
                    if isinstance(c, dict)
                )
            else:
                failing = False
            if failing:
                items.append({
                    "id": _make_id(pr_id_base, "ci-fail"),
                    "severity": "critical",
                    "source": "pr",
                    "title": f"CI failing — {title}",
                    "detail": f"{project} #{pr_number}",
                    "url": url,
                    "age_seconds": age,
                    "created_at": created_at,
                })
                continue  # don't also emit review/stale for same PR

            # Review required
            review_decision = pr.get("reviewDecision", "")
            if review_decision == "REVIEW_REQUIRED":
                items.append({
                    "id": _make_id(pr_id_base, "review-required"),
                    "severity": "warning",
                    "source": "pr",
                    "title": f"Review required — {title}",
                    "detail": f"{project} #{pr_number}",
                    "url": url,
                    "age_seconds": age,
                    "created_at": created_at,
                })
                continue

            # Stale PR
            if age > _STALE_PR_SECONDS:
                items.append({
                    "id": _make_id(pr_id_base, "stale"),
                    "severity": "warning",
                    "source": "pr",
                    "title": f"Stale PR — {title}",
                    "detail": f"{project} #{pr_number} · {age // 86400}d old",
                    "url": url,
                    "age_seconds": age,
                    "created_at": created_at,
                })
    except Exception as e:
        print(f"[attention] error collecting PR items: {e}", file=sys.stderr)
    return items


def _collect_quota_items(koan_root: str) -> list:
    """Return attention item if quota pause is active."""
    items = []
    try:
        root = Path(koan_root)
        if (root / QUOTA_RESET_FILE).exists() or (root / PAUSE_FILE).exists():
            # Check if quota-related
            pause_file = root / PAUSE_FILE
            is_quota = (root / QUOTA_RESET_FILE).exists()
            if not is_quota and pause_file.exists():
                try:
                    from app.pause_manager import get_pause_state
                    state = get_pause_state(koan_root)
                    if state and state.reason == "quota":
                        is_quota = True
                except Exception as e:
                    print(f"[attention] error reading pause state: {e}", file=sys.stderr)
            if is_quota:
                items.append({
                    "id": _make_id("quota-pause"),
                    "severity": "warning",
                    "source": "quota",
                    "title": "Quota paused",
                    "detail": "API quota exhausted — agent is waiting for reset",
                    "url": "/",
                    "age_seconds": 0,
                    "created_at": "",
                })
    except Exception as e:
        print(f"[attention] error collecting quota items: {e}", file=sys.stderr)
    return items


def _collect_github_mention_items(koan_root: str) -> list:
    """Return attention items from unread GitHub @mention notifications.

    Gated by ``attention_github_notifications: true`` in config.yaml.
    Skips silently when GitHub auth is not configured.
    """
    global _github_auth_warned
    items = []
    try:
        from app.utils import load_config
        config = load_config()
        if not config.get("attention_github_notifications", False):
            return []

        from app.github_notifications import fetch_unread_notifications
        from app.projects_config import load_projects_config, get_projects_from_config

        proj_cfg = load_projects_config(koan_root)
        known_repos: set = set()
        if proj_cfg:
            for name, _path in get_projects_from_config(proj_cfg):
                from app.projects_config import get_project_config
                pc = get_project_config(proj_cfg, name)
                url = pc.get("github_url", "")
                if url:
                    known_repos.add(url.lower())

        result = fetch_unread_notifications(known_repos=known_repos or None)
        for notif in result.actionable:
            reason = notif.get("reason", "")
            if reason not in ("mention", "review_requested"):
                continue
            repo = (notif.get("repository") or {}).get("full_name", "")
            subject = notif.get("subject") or {}
            title = subject.get("title", "Notification")
            url = subject.get("url", "")
            notif_id = str(notif.get("id", ""))
            updated_at = notif.get("updated_at", "")
            age = _age_seconds(updated_at)
            items.append({
                "id": _make_id("gh-mention", notif_id),
                "severity": "info",
                "source": "github",
                "title": f"@mention — {title}",
                "detail": repo,
                "url": url,
                "age_seconds": age,
                "created_at": updated_at,
            })
    except RuntimeError:
        # GitHub auth not configured
        if not _github_auth_warned:
            print("[attention] GitHub auth not configured — skipping @mention items", file=sys.stderr)
            _github_auth_warned = True
    except Exception as e:
        print(f"[attention] error collecting GitHub notifications: {e}", file=sys.stderr)
    return items


# ---------------------------------------------------------------------------
# Main aggregator
# ---------------------------------------------------------------------------

def get_attention_items(koan_root: str, project_filter: str = "") -> list:
    """Aggregate attention items from all sources.

    Returns a list of at most 20 items, sorted by severity then age descending.
    Dismissed items are filtered out.
    """
    global _attention_cache

    # Cache check (30s TTL) — but dismissal is always applied fresh
    now = time.monotonic()
    if _attention_cache and (now - _attention_cache[1]) < _ATTENTION_CACHE_TTL:
        raw_items = _attention_cache[0]
    else:
        raw_items = []
        raw_items.extend(_collect_failed_missions(koan_root))
        raw_items.extend(_collect_pr_items(koan_root, project_filter))
        raw_items.extend(_collect_quota_items(koan_root))
        raw_items.extend(_collect_github_mention_items(koan_root))
        _attention_cache = (raw_items, now)

    dismissed = load_dismissed(koan_root)
    filtered = [item for item in raw_items if item["id"] not in dismissed]

    # Sort: critical first, then warning, then info; within severity by age desc
    filtered.sort(
        key=lambda x: (_SEVERITY_ORDER.get(x["severity"], 99), -x["age_seconds"])
    )

    return filtered[:20]


def get_attention_count(koan_root: str) -> int:
    """Return the count of non-dismissed attention items (cached, cheap)."""
    return len(get_attention_items(koan_root))
