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
from typing import Callable, List, Optional, Sequence, Tuple

from app import jira_notifications as _jira_notifications
from app.github_url_parser import parse_jira_url
from app.jira_notifications import (
    jira_add_comment,
    jira_comment_authored_by_self,
    jira_edit_comment,
    jira_list_comments_checked,
)
from app.run_log import log_safe as _log_runner
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
# The footer is plain text, so a human can reproduce it by quoting the tail of a
# plan. A Jira comment entity property cannot be produced from the comment
# editor — it only exists if something wrote it through the REST comment
# payload — which makes it the authorship proof the footer can never be.
_PLAN_PROPERTY_KEY = "koan.jira.plan"

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


def _part_header(part_number: int, part_count: int) -> str:
    return f"{_FOOTER_LABEL} — Part {part_number} of {part_count}"


# Readers (notably the `implement` skill, which reassembles a split plan) must
# recognise exactly what the renderer above emits. Both directions live here so
# a change to the format cannot silently strand a consumer on the old shape.
_HEADER_LINE_RE = re.compile(
    rf"^{re.escape(_FOOTER_LABEL)} — Part \d+ of \d+\s*$", re.MULTILINE,
)
_NAVIGATION_LINE_RE = re.compile(
    r"^\s*(?:Previous|Next) part: https?://\S+\s*$", re.MULTILINE,
)
_FOOTER_LINE_RE = re.compile(
    rf"^{re.escape(_FOOTER_LABEL)} \(rev [0-9a-f]{{16}}(?:, part \d+/\d+)?\)\s*$",
    re.MULTILINE,
)


def parse_plan_comment(comment_body: str) -> Optional[Tuple[str, int, int]]:
    """Return ``(revision, part_number, part_count)`` for a Koan plan comment.

    ``None`` when the body is not one. A single-part plan reports ``(rev, 1, 1)``.
    """
    match = _FOOTER_RE.search((comment_body or "").rstrip())
    if not match:
        return None
    return match.group(1), int(match.group(2) or 1), int(match.group(3) or 1)


def strip_plan_envelope(comment_body: str) -> str:
    """Drop the part header, navigation links, and footer, keeping plan content."""
    text = _HEADER_LINE_RE.sub("", comment_body or "", count=1)
    text = _NAVIGATION_LINE_RE.sub("", text)
    return _FOOTER_LINE_RE.sub("", text)


def _split_comment_body(comment_body: str) -> List[str]:
    """Split an oversized plan at paragraph, then line, then word boundaries."""
    if len(comment_body) <= _PART_BODY_CHARS:
        return [comment_body]

    parts: List[str] = []
    remaining = comment_body
    # Only accept a boundary in the back half of the window. Preferring the
    # coarsest separator outright collapses on real plans: a File Map table is a
    # long blank-line-free run, so the last "\n\n" can sit near the very start
    # and would emit an absurd 40-character "Part 1 of N" plus a needless extra
    # publish. Below the floor, fall through to a finer separator.
    floor = _PART_BODY_CHARS // 2
    while len(remaining) > _PART_BODY_CHARS:
        cut = _PART_BODY_CHARS
        for separator in ("\n\n", "\n", " "):
            candidate = remaining.rfind(separator, 0, cut)
            if candidate > floor:
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
    header = f"{_part_header(part_number, part_count)}\n\n" if part_count > 1 else ""
    nav_block = f"\n\n{navigation.strip()}" if navigation.strip() else ""
    rendered = (
        f"{header}{part.rstrip()}{nav_block}\n\n"
        f"{_footer_for(revision, part_number, part_count)}"
    )
    if len(rendered) > _MAX_COMMENT_CHARS:
        raise ValueError(f"Rendered Jira plan part exceeds {_MAX_COMMENT_CHARS} characters")
    return rendered


def _plan_properties(revision: str, part_number: int, part_count: int) -> List[dict]:
    """The entity property stamped on every plan comment Koan writes."""
    return [{
        "key": _PLAN_PROPERTY_KEY,
        "value": {"revision": revision, "part": part_number, "parts": part_count},
    }]


def _authored_by_koan(comment: dict) -> bool:
    properties = comment.get("properties")
    return isinstance(properties, dict) and _PLAN_PROPERTY_KEY in properties


def _provably_koan(comment: dict) -> bool:
    """Authorship Koan can prove before overwriting a comment body."""
    return (
        _authored_by_koan(comment)
        or jira_comment_authored_by_self(comment) is True
    )


def koan_authorship_check(comments, strict: bool = False) -> Callable[[dict], bool]:
    """"Did Koan write this comment?", keyed on the plan entity property.

    See :func:`app.jira_notifications.koan_authorship_check` for the rule and
    for what ``strict`` buys a caller that is about to replace a comment body;
    the plan comment's proof of authorship is the ``koan.jira.plan`` property.
    """
    return _jira_notifications.koan_authorship_check(
        comments, _PLAN_PROPERTY_KEY, strict=strict,
    )


def _find_plan_comments(
    comments, strict: bool = False,
) -> List[Tuple[dict, str, int, int]]:
    """Return every Koan plan comment as ``(comment, revision, part, count)``.

    Identity is authorship (see :func:`koan_authorship_check`) *and* the
    trailing footer. Authorship answers "is this ours?" — a reviewer who pastes
    the tail of a plan into their own comment ends it with the footer, and
    without the guard that comment becomes what the next revision edits and
    what the retirement pass blanks out. The footer answers "which revision and
    part?" and is still matched at the end of the body, so a plan quoted
    mid-body is not mistaken for a plan comment either.

    ``strict`` is for callers selecting a comment to *overwrite*: it demands
    proof of authorship rather than the absence of a foreign one, so a tenant
    that answers neither ``expand=properties`` nor ``/myself`` costs a stray
    plan part instead of a reviewer's text.
    """
    authored_by_koan = koan_authorship_check(comments, strict=strict)

    found = []
    for comment in comments or []:
        if not authored_by_koan(comment):
            continue
        match = _FOOTER_RE.search((comment.get("body") or "").rstrip())
        if match:
            revision, part, count = match.group(1), match.group(2), match.group(3)
            found.append((comment, revision, int(part or 1), int(count or 1)))
    return found


def _locate_part(comments, part_number: int) -> Optional[dict]:
    """The comment currently holding part N, whatever revision it carries.

    Revision-agnostic on purpose: a new plan revision must *update* the comment
    holding that part rather than post a fresh one beside it.

    Its only caller edits what it returns, so authorship must be proven
    (``strict``) rather than merely unrefuted.
    """
    for comment, _rev, part, _count in _find_plan_comments(comments, strict=True):
        if part == part_number:
            return comment
    return None


def _verify_part(comments, revision: str, part_number: int) -> Optional[dict]:
    """The comment proving Jira holds this exact revision of part N."""
    for comment, rev, part, _count in _find_plan_comments(comments):
        if rev == revision and part == part_number:
            return comment
    return None


def _stage_unreadable(issue_url: str, reason: str) -> None:
    """Report a stage that exists but cannot be used.

    Silence here would be indistinguishable from "nothing was staged", and what
    is lost is the model run this whole module exists to protect.
    """
    issue_key = ""
    with suppress(Exception):
        issue_key = parse_jira_url(issue_url)
    _audit(issue_key, "stage_read", "failure", 1, error=reason[:180])


def _read_stage(issue_url: str, instance_dir: str) -> Optional[dict]:
    path = stage_path_for(issue_url, instance_dir)
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError) as exc:
        _stage_unreadable(issue_url, f"{type(exc).__name__}: {exc}")
        return None
    if not isinstance(data, dict):
        _stage_unreadable(issue_url, "staged payload is not an object")
        return None
    if data.get("issue_url") != issue_url or not isinstance(data.get("comment_body"), str):
        _stage_unreadable(issue_url, "staged payload is missing issue_url/comment_body")
        return None
    return data


def _write_stage(issue_url: str, instance_dir: str, payload: dict) -> None:
    path = stage_path_for(issue_url, instance_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _clear_staged_plan(issue_url: str, instance_dir: str) -> bool:
    """Delete the stage, reporting whether it is actually gone.

    A swallowed failure here is not cosmetic: the stage surviving means the
    next `/plan` on this issue resumes and republishes it instead of generating
    the plan the user just asked for. Callers must not claim the stage was
    cleared or abandoned unless this returns True.
    """
    path = stage_path_for(issue_url, instance_dir)
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError as exc:
        issue_key = ""
        with suppress(Exception):
            issue_key = parse_jira_url(issue_url)
        _audit(
            issue_key, "stage_clear", "failure", 1,
            error=str(exc)[:180], stage_path=str(path),
        )
        return False


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
        # Report absent whether or not the delete lands. Returning the body
        # instead would resurrect exactly the undeliverable plan the expiry
        # exists to retire, and a surviving file is harmless here: the next
        # stage_plan() overwrites this same path. The failed delete is audited
        # by _clear_staged_plan rather than passed back.
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
    if result == "failure":
        # log_event returns early when auditing is disabled, so a failure whose
        # only trace is security.jsonl is invisible to whoever runs `make logs`.
        extra = " ".join(f"{k}={v}" for k, v in details.items() if v not in (None, ""))
        _log_runner(
            "jira",
            f"plan {action} failed for {issue_key or '?'} "
            f"(attempt {attempt}){': ' + extra if extra else ''}",
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
        # Only report abandonment if the stage is genuinely gone; otherwise the
        # next run would replay a plan we just told the operator we dropped.
        if _clear_staged_plan(issue_url, instance_dir):
            return False, f"abandoned_after_{sessions}_failed_runs"

    data["sessions"] = sessions
    _write_stage(issue_url, instance_dir, data)
    return False, reason


def _contains_navigation(body: str, navigation: str) -> bool:
    """Whether a read-back comment already carries these navigation links.

    Compared on collapsed whitespace: the body comes back through ADF, which
    splits a linked URL into its own text node and re-joins siblings with a
    space. A literal substring test never matches that, so every part would be
    rewritten on every run and re-notify every watcher.
    """
    if not navigation.strip():
        return True
    return " ".join(navigation.split()) in " ".join(body.split())


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

    Returns ``(published, detail)``: the Jira comment id on success, and
    otherwise a machine-readable failure reason (``""`` for the generic
    verification failure the caller names itself).
    """
    try:
        rendered = _render_comment(part, revision, part_number, part_count, navigation)
    except ValueError as exc:
        _audit(issue_key, "render", "failure", 0, error=str(exc)[:180], part=part_number)
        return False, ""

    properties = _plan_properties(revision, part_number, part_count)
    created_unverified = False
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
            not always_write or _contains_navigation(settled.get("body") or "", navigation)
        ):
            _audit(
                issue_key, "verify", "success", attempt,
                comment_id=settled.get("id", ""), part=part_number, parts=part_count,
            )
            return True, str(settled.get("id", ""))

        # `settled` came from a read-only match, but from here on it is an edit
        # target — the navigation pass rewrites the comment it already verified.
        # Only a comment Koan can prove it wrote may be overwritten.
        existing = settled if settled is not None and _provably_koan(settled) else None
        if existing is None:
            existing = _locate_part(comments, part_number)
        if existing is None and created_unverified:
            # An earlier attempt already tried to create this part and the
            # comment is still not in the listing. Jira's comment read path is
            # not read-your-writes, so this is at least as likely to be a
            # lagging replica as a write that never landed — and creating again
            # is exactly how one plan part becomes two, each notifying every
            # watcher. Stop and let the next mission run re-verify; the stage is
            # kept, so no model run is lost.
            _audit(
                issue_key, "create", "failure", attempt,
                error="create attempted but never read back",
                part=part_number, parts=part_count,
            )
            return False, "created_unverified"

        action = "update" if existing is not None else "create"
        if action == "create":
            # Arm the guard on the *attempt*, not on its reported result: a POST
            # whose response is lost (socket timeout, dropped connection) is
            # reported as a failure by `jira_add_comment` even though Jira
            # created the comment. Trusting that report is how a duplicate
            # `Part N of M` gets posted on the next attempt.
            created_unverified = True
        try:
            ok = (
                jira_edit_comment(
                    issue_key, str(existing.get("id", "")), rendered, properties=properties,
                )
                if existing is not None
                else jira_add_comment(issue_key, rendered, properties=properties)
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

        # The navigation pass edits a comment that already carries this revision,
        # so a revision match alone proves nothing about whether the edit landed.
        # Read back the artifact this write was for — the links themselves.
        if verified is not None and always_write and not _contains_navigation(
            verified.get("body") or "", navigation
        ):
            verified = None

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


def _retire_superseded_parts(
    issue_key: str,
    revision: str,
    part_count: int,
    published_ids: Optional[Sequence[str]] = None,
) -> bool:
    """Blank out plan comments left behind by an earlier, longer plan.

    Without this, shrinking a 3-part plan to 2 parts strands part 3 on the issue
    with stale content and a dangling "previous part" link — and because
    `/implement` looks for multipart groups before single-part plans, that
    stranded group is what it would implement.

    Jira exposes no comment delete here, so the body is replaced and its footer
    dropped, which stops the comment being matched as a plan part. Returns True
    only once a read-back shows no superseded part remains: Jira's write
    endpoints report success for writes that never landed, so an unverified
    retirement must not let the caller declare the publish complete.

    This pass destroys a comment body, so it is the one place that demands
    *proof* of authorship — the ``koan.jira.plan`` property, or Jira naming
    Koan's own account as the author. A comment that merely looks like a stale
    part (a human quoting an older plan on a property-less deployment) is left
    untouched: a stranded part is recoverable, an overwritten human comment is
    not.

    ``published_ids`` are the comments this publish just wrote *and* read-back
    verified. They are never orphans, whatever this fresh listing says: Jira's
    comment read path is not read-your-writes (see :func:`_upsert_part`), so a
    lagging replica can still be serving the pre-edit body — old revision,
    ``koan.jira.plan`` property intact — and without this guard the pass would
    blank out the plan it just published, then read no footer on it and report
    the retirement as complete.
    """
    protected = {str(comment_id) for comment_id in (published_ids or []) if comment_id}

    def superseded(comments):
        return [
            comment for comment, rev, part, _count in _find_plan_comments(comments)
            if (rev != revision or part > part_count)
            and str(comment.get("id", "")) not in protected
            and _provably_koan(comment)
        ]

    try:
        orphans = superseded(jira_list_comments_checked(issue_key))
    except Exception as exc:
        _audit(issue_key, "retire", "failure", 1, error=str(exc)[:180])
        return False

    if not orphans:
        return True

    for comment in orphans:
        comment_id = str(comment.get("id", ""))
        try:
            jira_edit_comment(issue_key, comment_id, _SUPERSEDED_BODY)
        except Exception as exc:
            _audit(issue_key, "retire", "failure", 1, comment_id=comment_id, error=str(exc)[:180])

    try:
        remaining = superseded(jira_list_comments_checked(issue_key))
    except Exception as exc:
        _audit(issue_key, "retire", "failure", 1, error=str(exc)[:180])
        return False

    _audit(
        issue_key, "retire", "success" if not remaining else "failure", 1,
        retired=len(orphans) - len(remaining), remaining=len(remaining),
    )
    return not remaining


def publish_staged_plan(
    issue_url: str,
    instance_dir: str = "",
    attempts: int = _PUBLISH_ATTEMPTS,
) -> Tuple[bool, str]:
    """Publish and read-back verify the staged plan comment(s) for ``issue_url``.

    The ``koan.jira.plan`` property plus the footer make an ordinary retry an
    update of Koan's own existing plan comment rather than a second one, and
    the footer's revision proves Jira is holding this exact staged plan before
    success is reported. A failed verification deliberately leaves the staged
    artifact intact so the next mission run does not have to regenerate the
    plan.

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
        posted, detail = _upsert_part(
            issue_key, revision, part, index + 1, part_count, "", attempts,
        )
        if not posted:
            return failure(index + 1, detail or "verification_failed")
        comment_ids.append(detail)

    if part_count > 1:
        for index, part in enumerate(parts):
            posted, detail = _upsert_part(
                issue_key, revision, part, index + 1, part_count,
                _navigation(issue_url, comment_ids, index), attempts,
                always_write=True,
            )
            if not posted:
                return failure(index + 1, detail or "navigation_failed")
            comment_ids[index] = detail

    # Keep the stage until cleanup is verified. A stranded older group would be
    # picked up by `/implement` in preference to this revision, so the publish
    # is not finished while one survives — the next run resumes and retries.
    if not _retire_superseded_parts(issue_key, revision, part_count, comment_ids):
        return _record_failed_session(
            issue_url, instance_dir, "superseded_parts_not_retired",
        )

    if not _clear_staged_plan(issue_url, instance_dir):
        # The comments are verified, but a surviving stage makes the next
        # `/plan` on this issue resume and republish it instead of generating
        # the plan that was asked for. Report the inconsistency instead of a
        # clean success; the next run re-verifies cheaply (no model call) and
        # retries the delete. Deliberately not counted as a failed publish
        # session — the publish itself worked.
        return False, "stage_clear_failed"

    return True, ", ".join(comment_ids)
