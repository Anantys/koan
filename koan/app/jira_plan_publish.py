"""Verified, resumable publishing of the current Jira plan comment.

A plan is an expensive model run, so it is staged on disk before Koan tries to
deliver it. Delivery is then verified by reading the comment back: Jira's write
endpoints report success on responses that never became a visible comment, so
an unverified write is treated as a failure and the staged plan is kept for the
next mission run rather than regenerated.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from contextlib import suppress
from pathlib import Path
from typing import Optional, Tuple

from app.github_url_parser import parse_jira_url
from app.jira_notifications import (
    jira_add_comment,
    jira_edit_comment,
    jira_list_comments_checked,
)
from app.security_audit import TRACKER_COMMENT_MUTATION, log_event
from app.utils import atomic_write

# The plan comment carries a human-readable footer rather than an HTML comment:
# Jira renders ADF text literally, so an `<!-- ... -->` marker would show up as
# visible gibberish. The footer doubles as the dedup key (find the previous plan
# comment) and the read-back proof (its revision is a digest of the plan body).
_FOOTER_LABEL = "Koan current plan"
_FOOTER_RE = re.compile(rf"{re.escape(_FOOTER_LABEL)} \(rev ([0-9a-f]{{16}})\)\s*$")

_PUBLISH_ATTEMPTS = 3
# Publishing is retried across mission runs, but a permanently broken Jira must
# not wedge the issue forever: after this many failed runs the stage is dropped
# so the next `/plan` regenerates instead of replaying a stale plan.
_MAX_PUBLISH_SESSIONS = 3
_STAGE_MAX_AGE_SECONDS = 7 * 24 * 3600


def _instance_path(instance_dir: str) -> Path:
    if instance_dir:
        return Path(instance_dir)
    return Path(os.environ.get("KOAN_ROOT", ".")) / "instance"


def stage_path_for(issue_url: str, instance_dir: str = "") -> Path:
    """Return the on-disk staging path for an issue's pending plan comment."""
    digest = hashlib.sha256(issue_url.encode("utf-8")).hexdigest()[:20]
    return _instance_path(instance_dir) / "pending-jira-plan-publishes" / f"{digest}.json"


def _revision(comment_body: str) -> str:
    return hashlib.sha256(comment_body.encode("utf-8")).hexdigest()[:16]


def _footer_for(comment_body: str) -> str:
    return f"{_FOOTER_LABEL} (rev {_revision(comment_body)})"


def _render_comment(comment_body: str) -> str:
    return f"{comment_body.rstrip()}\n\n{_footer_for(comment_body)}"


def _find_plan_comment(comments) -> Tuple[Optional[dict], str]:
    """Return the existing Koan plan comment and its revision, if present.

    The footer is matched at the end of the body so a plan that merely quotes
    the footer text mid-body is not mistaken for the plan comment itself.
    """
    for comment in comments or []:
        match = _FOOTER_RE.search((comment.get("body") or "").rstrip())
        if match:
            return comment, match.group(1)
    return None, ""


def _read_stage(issue_url: str, instance_dir: str) -> Optional[dict]:
    try:
        data = json.loads(stage_path_for(issue_url, instance_dir).read_text())
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("issue_url") != issue_url or not isinstance(data.get("comment_body"), str):
        return None
    return data


def _write_stage(issue_url: str, instance_dir: str, payload: dict) -> None:
    path = stage_path_for(issue_url, instance_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _clear_staged_plan(issue_url: str, instance_dir: str) -> None:
    with suppress(OSError):
        stage_path_for(issue_url, instance_dir).unlink(missing_ok=True)


def stage_plan(issue_url: str, comment_body: str, instance_dir: str = "") -> None:
    """Atomically persist a generated plan before trying to publish it."""
    _write_stage(issue_url, instance_dir, {
        "issue_url": issue_url,
        "issue_key": parse_jira_url(issue_url),
        "comment_body": comment_body,
        "staged_at": time.time(),
        "sessions": 0,
    })


def load_staged_plan(issue_url: str, instance_dir: str = "") -> Optional[str]:
    """Return the pending plan body for this issue, if a publish needs resuming.

    An expired stage is discarded (and reported absent) so a permanently
    undeliverable plan eventually gives way to a freshly generated one.
    """
    data = _read_stage(issue_url, instance_dir)
    if data is None:
        return None

    staged_at = data.get("staged_at")
    if isinstance(staged_at, (int, float)) and time.time() - staged_at > _STAGE_MAX_AGE_SECONDS:
        _clear_staged_plan(issue_url, instance_dir)
        return None
    return data["comment_body"]


def _audit(issue_key: str, action: str, result: str, attempt: int, **details) -> None:
    log_event(
        TRACKER_COMMENT_MUTATION,
        result=result,
        details={
            "provider": "jira", "issue_key": issue_key, "action": action,
            "attempt": attempt, **details,
        },
    )


def _record_failed_session(issue_url: str, instance_dir: str) -> Tuple[bool, str]:
    """Count a failed publish run, abandoning the stage once the cap is hit."""
    data = _read_stage(issue_url, instance_dir)
    if data is None:
        return False, "verification_failed"

    sessions = int(data.get("sessions") or 0) + 1
    if sessions >= _MAX_PUBLISH_SESSIONS:
        _clear_staged_plan(issue_url, instance_dir)
        return False, f"abandoned_after_{sessions}_failed_runs"

    data["sessions"] = sessions
    _write_stage(issue_url, instance_dir, data)
    return False, "verification_failed"


def publish_staged_plan(
    issue_url: str,
    instance_dir: str = "",
    attempts: int = _PUBLISH_ATTEMPTS,
) -> Tuple[bool, str]:
    """Publish and read-back verify the staged plan comment for ``issue_url``.

    The footer makes an ordinary retry an update of the existing plan comment
    rather than a second one, and its revision proves Jira is holding this exact
    staged plan before success is reported. A failed verification deliberately
    leaves the staged artifact intact so the next mission run does not have to
    regenerate the plan.

    Returns ``(published, detail)`` where ``detail`` is the Jira comment id on
    success, and otherwise a machine-readable failure reason.
    """
    comment_body = load_staged_plan(issue_url, instance_dir)
    if comment_body is None:
        return False, "no_staged_plan"

    issue_key = parse_jira_url(issue_url)
    expected_rev = _revision(comment_body)
    rendered = _render_comment(comment_body)

    for attempt in range(1, max(1, attempts) + 1):
        # A failed lookup is not "no plan comment yet" — creating one here is how
        # a flaky read path turns into a pile of duplicate plan comments.
        try:
            existing, rev = _find_plan_comment(jira_list_comments_checked(issue_key))
        except Exception as exc:
            _audit(issue_key, "lookup", "failure", attempt, error=str(exc)[:180])
            if attempt < attempts:
                time.sleep(attempt)
            continue

        if existing is not None and rev == expected_rev:
            _audit(issue_key, "verify", "success", attempt, comment_id=existing.get("id", ""))
            _clear_staged_plan(issue_url, instance_dir)
            return True, str(existing.get("id", ""))

        action = "update" if existing is not None else "create"
        try:
            ok = (
                jira_edit_comment(issue_key, str(existing.get("id", "")), rendered)
                if existing is not None
                else jira_add_comment(issue_key, rendered)
            )
        except Exception as exc:
            ok = False
            _audit(issue_key, action, "failure", attempt, error=str(exc)[:180])
        else:
            _audit(issue_key, action, "success" if ok else "failure", attempt)

        try:
            verified, rev = _find_plan_comment(jira_list_comments_checked(issue_key))
        except Exception as exc:
            verified, rev = None, ""
            _audit(issue_key, "verify", "failure", attempt, error=str(exc)[:180])

        if verified is not None and rev == expected_rev:
            _audit(issue_key, "verify", "success", attempt, comment_id=verified.get("id", ""))
            _clear_staged_plan(issue_url, instance_dir)
            return True, str(verified.get("id", ""))

        _audit(issue_key, "verify", "failure", attempt, post_result=bool(ok))
        if attempt < attempts:
            time.sleep(attempt)

    return _record_failed_session(issue_url, instance_dir)
