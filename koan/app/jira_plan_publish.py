"""Verified, resumable publishing of the current Jira plan comment.

A plan is an expensive model run, so it is staged on disk before Koan tries to
deliver it. Delivery is then verified by reading the comment back: Jira's write
endpoints report success on responses that never became a visible comment, so
an unverified write is treated as a failure and the staged plan is kept for the
next mission run rather than regenerated.

Plans too large for a single Jira comment are published as consecutive parts.
Jira's public REST API has no reply-to-comment operation, so the parts are
linked to each other with focused-comment URLs instead of being threaded.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from contextlib import suppress
from pathlib import Path
from typing import List, Optional, Tuple

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
# comment) and the read-back proof: `rev` is a digest of the whole staged plan,
# so every part of one plan shares it and parts left over from an older plan are
# recognisable as stale.
_FOOTER_LABEL = "Koan current plan"
_FOOTER_RE = re.compile(
    rf"{re.escape(_FOOTER_LABEL)} \(rev ([0-9a-f]{{16}})(?:, part (\d+)/(\d+))?\)\s*$"
)
_SUPERSEDED_BODY = "(Superseded — this part of an earlier Koan plan was replaced.)"
_FENCE_RE = re.compile(r"^\s*```(.*)$")

_PUBLISH_ATTEMPTS = 3
# Jira rejects comments beyond roughly 32k characters. Split well under that so
# the part header, navigation links, and footer always fit in the remainder.
_MAX_COMMENT_CHARS = 30_000
_PART_BODY_CHARS = 29_000
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


def _footer_for(revision: str, part_number: int = 1, part_count: int = 1) -> str:
    if part_count > 1:
        return f"{_FOOTER_LABEL} (rev {revision}, part {part_number}/{part_count})"
    return f"{_FOOTER_LABEL} (rev {revision})"


def _split_comment_body(comment_body: str) -> List[str]:
    """Split an oversized plan at paragraph, then line, then word boundaries."""
    if len(comment_body) <= _PART_BODY_CHARS:
        return [comment_body]

    parts: List[str] = []
    remaining = comment_body
    while len(remaining) > _PART_BODY_CHARS:
        cut = _PART_BODY_CHARS
        for separator in ("\n\n", "\n", " "):
            candidate = remaining.rfind(separator, 0, cut)
            if candidate > 0:
                cut = candidate + len(separator)
                break
        parts.append(remaining[:cut])
        remaining = remaining[cut:]
    parts.append(remaining)
    return parts


def _fence_balanced(parts: List[str]) -> List[str]:
    """Close a code fence left open by a split, and reopen it in the next part.

    Comments are rendered with ``markdown_to_adf`` and read back with
    ``_adf_to_text``, which drops ``codeBlock`` content. A part cut mid-fence
    would therefore swallow its own verification footer and never verify.
    """
    balanced: List[str] = []
    reopen = ""
    for part in parts:
        body = reopen + part
        open_lang: Optional[str] = None
        for line in body.splitlines():
            match = _FENCE_RE.match(line)
            if match:
                open_lang = None if open_lang is not None else match.group(1).strip()
        if open_lang is None:
            reopen = ""
        else:
            body = f"{body.rstrip()}\n```"
            reopen = f"```{open_lang}\n"
        balanced.append(body)
    return balanced


def _plan_parts(comment_body: str) -> List[str]:
    """The comment bodies to publish for a plan, each independently renderable."""
    return _fence_balanced(_split_comment_body(comment_body))


def _navigation(issue_url: str, comment_ids: List[str], index: int) -> str:
    """Build previous/next links; Jira cannot thread a reply under a comment."""
    if len(comment_ids) < 2:
        return ""
    links = []
    if index > 0:
        links.append(f"Previous part: {issue_url}?focusedCommentId={comment_ids[index - 1]}")
    if index + 1 < len(comment_ids):
        links.append(f"Next part: {issue_url}?focusedCommentId={comment_ids[index + 1]}")
    return "\n".join(links)


def _render_comment(
    part: str,
    revision: str,
    part_number: int = 1,
    part_count: int = 1,
    navigation: str = "",
) -> str:
    header = f"{_FOOTER_LABEL} — Part {part_number} of {part_count}\n\n" if part_count > 1 else ""
    nav_block = f"\n\n{navigation.strip()}" if navigation.strip() else ""
    rendered = (
        f"{header}{part.rstrip()}{nav_block}\n\n"
        f"{_footer_for(revision, part_number, part_count)}"
    )
    if len(rendered) > _MAX_COMMENT_CHARS:
        raise ValueError(f"Rendered Jira plan part exceeds {_MAX_COMMENT_CHARS} characters")
    return rendered


def _find_plan_comments(comments) -> List[Tuple[dict, str, int, int]]:
    """Return every Koan plan comment as ``(comment, revision, part, count)``.

    The footer is matched at the end of the body so a plan that merely quotes
    the footer text mid-body is not mistaken for a plan comment itself.
    """
    found = []
    for comment in comments or []:
        match = _FOOTER_RE.search((comment.get("body") or "").rstrip())
        if match:
            revision, part, count = match.group(1), match.group(2), match.group(3)
            found.append((comment, revision, int(part or 1), int(count or 1)))
    return found


def _locate_part(comments, part_number: int) -> Optional[dict]:
    """The comment currently holding part N, whatever revision it carries.

    Revision-agnostic on purpose: a new plan revision must *update* the comment
    holding that part rather than post a fresh one beside it.
    """
    for comment, _rev, part, _count in _find_plan_comments(comments):
        if part == part_number:
            return comment
    return None


def _verify_part(comments, revision: str, part_number: int) -> Optional[dict]:
    """The comment proving Jira holds this exact revision of part N."""
    for comment, rev, part, _count in _find_plan_comments(comments):
        if rev == revision and part == part_number:
            return comment
    return None


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


def _record_failed_session(
    issue_url: str,
    instance_dir: str,
    reason: str = "verification_failed",
) -> Tuple[bool, str]:
    """Count a failed publish run, abandoning the stage once the cap is hit."""
    data = _read_stage(issue_url, instance_dir)
    if data is None:
        return False, reason

    sessions = int(data.get("sessions") or 0) + 1
    if sessions >= _MAX_PUBLISH_SESSIONS:
        _clear_staged_plan(issue_url, instance_dir)
        return False, f"abandoned_after_{sessions}_failed_runs"

    data["sessions"] = sessions
    _write_stage(issue_url, instance_dir, data)
    return False, reason


def _upsert_part(
    issue_key: str,
    revision: str,
    part: str,
    part_number: int,
    part_count: int,
    navigation: str,
    attempts: int,
    always_write: bool = False,
) -> Tuple[bool, str]:
    """Create or update one plan part, requiring a Jira read-back match.

    ``always_write`` forces the edit used to attach navigation links, whose
    targets are only known once every part has an id.
    """
    try:
        rendered = _render_comment(part, revision, part_number, part_count, navigation)
    except ValueError as exc:
        _audit(issue_key, "render", "failure", 0, error=str(exc)[:180], part=part_number)
        return False, ""

    for attempt in range(1, max(1, attempts) + 1):
        # A failed lookup is not "no plan comment yet" — creating one here is how
        # a flaky read path turns into a pile of duplicate plan comments.
        try:
            comments = jira_list_comments_checked(issue_key)
        except Exception as exc:
            _audit(issue_key, "lookup", "failure", attempt, error=str(exc)[:180], part=part_number)
            if attempt < attempts:
                time.sleep(attempt)
            continue

        settled = _verify_part(comments, revision, part_number)
        if settled is not None and (
            not always_write or navigation.strip() in (settled.get("body") or "")
        ):
            _audit(
                issue_key, "verify", "success", attempt,
                comment_id=settled.get("id", ""), part=part_number, parts=part_count,
            )
            return True, str(settled.get("id", ""))

        existing = settled or _locate_part(comments, part_number)
        action = "update" if existing is not None else "create"
        try:
            ok = (
                jira_edit_comment(issue_key, str(existing.get("id", "")), rendered)
                if existing is not None
                else jira_add_comment(issue_key, rendered)
            )
        except Exception as exc:
            ok = False
            _audit(
                issue_key, action, "failure", attempt,
                error=str(exc)[:180], part=part_number, parts=part_count,
            )
        else:
            _audit(
                issue_key, action, "success" if ok else "failure", attempt,
                part=part_number, parts=part_count,
            )

        try:
            verified = _verify_part(jira_list_comments_checked(issue_key), revision, part_number)
        except Exception as exc:
            verified = None
            _audit(issue_key, "verify", "failure", attempt, error=str(exc)[:180], part=part_number)

        if verified is not None:
            _audit(
                issue_key, "verify", "success", attempt,
                comment_id=verified.get("id", ""), part=part_number, parts=part_count,
            )
            return True, str(verified.get("id", ""))

        _audit(
            issue_key, "verify", "failure", attempt,
            post_result=bool(ok), part=part_number, parts=part_count,
        )
        if attempt < attempts:
            time.sleep(attempt)

    return False, ""


def _retire_superseded_parts(issue_key: str, revision: str, part_count: int) -> None:
    """Blank out plan comments left behind by an earlier, longer plan.

    Without this, shrinking a 3-part plan to 2 parts strands part 3 on the issue
    with stale content and a dangling "previous part" link. Best-effort: Jira
    exposes no comment delete here, so the body is replaced and its footer
    dropped so the comment stops being matched as a plan part.
    """
    try:
        comments = jira_list_comments_checked(issue_key)
    except Exception:
        return

    for comment, rev, part, _count in _find_plan_comments(comments):
        if rev == revision and part <= part_count:
            continue
        with suppress(Exception):
            if jira_edit_comment(issue_key, str(comment.get("id", "")), _SUPERSEDED_BODY):
                _audit(issue_key, "retire", "success", 1, comment_id=comment.get("id", ""))


def publish_staged_plan(
    issue_url: str,
    instance_dir: str = "",
    attempts: int = _PUBLISH_ATTEMPTS,
) -> Tuple[bool, str]:
    """Publish and read-back verify the staged plan comment(s) for ``issue_url``.

    The footer makes an ordinary retry an update of the existing plan comment
    rather than a second one, and its revision proves Jira is holding this exact
    staged plan before success is reported. A failed verification deliberately
    leaves the staged artifact intact so the next mission run does not have to
    regenerate the plan.

    Oversized plans are published as consecutive parts, then revisited to attach
    previous/next links once every part id is known.

    Returns ``(published, detail)`` where ``detail`` is the Jira comment id — or
    comma-joined ids for a split plan — on success, and otherwise a
    machine-readable failure reason.
    """
    comment_body = load_staged_plan(issue_url, instance_dir)
    if comment_body is None:
        return False, "no_staged_plan"

    issue_key = parse_jira_url(issue_url)
    revision = _revision(comment_body)
    parts = _plan_parts(comment_body)
    part_count = len(parts)

    def failure(part_number: int, stage: str) -> Tuple[bool, str]:
        reason = (
            f"part_{part_number}_of_{part_count}_{stage}" if part_count > 1 else stage
        )
        return _record_failed_session(issue_url, instance_dir, reason)

    comment_ids: List[str] = []
    for index, part in enumerate(parts):
        posted, comment_id = _upsert_part(
            issue_key, revision, part, index + 1, part_count, "", attempts,
        )
        if not posted:
            return failure(index + 1, "verification_failed")
        comment_ids.append(comment_id)

    if part_count > 1:
        for index, part in enumerate(parts):
            posted, comment_id = _upsert_part(
                issue_key, revision, part, index + 1, part_count,
                _navigation(issue_url, comment_ids, index), attempts,
                always_write=True,
            )
            if not posted:
                return failure(index + 1, "navigation_failed")
            comment_ids[index] = comment_id

    _retire_superseded_parts(issue_key, revision, part_count)
    _clear_staged_plan(issue_url, instance_dir)
    return True, ", ".join(comment_ids)
