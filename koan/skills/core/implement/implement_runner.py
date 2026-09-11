"""
Kōan -- Implement runner.

Reads a GitHub issue containing a plan and invokes Claude to implement it.
The runner extracts the most recent plan iteration from the issue (body or
latest plan comment), ignoring older content, and feeds it to Claude with
an optional user-provided context (e.g. "Phase 1 to 3").

CLI:
    python3 -m skills.core.implement.implement_runner --project-path <path> --issue-url <url>
    python3 -m skills.core.implement.implement_runner --project-path <path> --issue-url <url> --context "Phase 1 to 3"
    python3 -m skills.core.implement.implement_runner --project-path <path> --project-name <name> --issue-url <url>
"""

import datetime
import hashlib
import logging
import re
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple, Union

from app.issue_tracker import (
    UnresolvedJiraProjectError,
    add_comment,
    fetch_issue,
    project_name_for_path,
)
from app.issue_tracker.config import resolve_code_repository
from app.jira_plan_publish import (
    koan_authorship_check,
    parse_plan_comment,
    strip_plan_envelope,
)
from app.pr_submit import (
    get_commit_subjects,
    get_current_branch,
    guess_project_name,
    submit_draft_pr,
)
from app.private_review_gate import format_gate_note, run_gate_for_skill
from app.prompts import load_prompt_or_skill

logger = logging.getLogger(__name__)

# Path to the plan skill directory (used for loading the plan-review prompt)
_PLAN_SKILL_DIR = Path(__file__).resolve().parent.parent / "plan"
_REVIEW_SKILL_DIR = Path(__file__).resolve().parent.parent / "review"


def _progress(msg: str) -> None:
    """Print a timestamped progress line to stdout.

    These lines are captured by ``_run_skill_mission`` in run.py and
    appended to ``pending.md``, making them visible via ``/live``.
    """
    ts = datetime.datetime.now().strftime("%H:%M")
    print(f"{ts} — {msg}", flush=True)


# Regex pattern matching plan structure markers
_PLAN_MARKER_RE = re.compile(
    r"^#{2,}\s+(?:Implementation Phases|Phase \d+|Summary|Changes in this iteration)",
    re.MULTILINE | re.IGNORECASE,
)


def _build_footer() -> str:
    from app.pr_footer import build_koan_footer
    return build_koan_footer()


def run_implement(
    project_path: str,
    issue_url: str,
    context: Optional[str] = None,
    notify_fn=None,
    skill_dir: Optional[Path] = None,
    base_branch: Optional[str] = None,
    project_name: str = "",
    instance_dir: str = "",
) -> Tuple[bool, str]:
    """Execute the implement pipeline.

    Fetches the GitHub issue, extracts the most recent plan, and invokes
    Claude to implement it.

    Args:
        project_path: Local path to the project repository.
        issue_url: GitHub issue URL containing the plan.
        context: Optional additional context (e.g. "Phase 1 to 3").
        notify_fn: Notification function (defaults to send_telegram).
        skill_dir: Path to the implement skill directory for prompt loading.

    Returns:
        (success, summary) tuple.
    """
    if notify_fn is None:
        from app.notify import send_telegram
        notify_fn = send_telegram

    context_label = f" ({context})" if context else ""
    project_name = project_name or project_name_for_path(project_path)

    _progress(f"Fetching tracker issue {issue_url}")

    # The tracker (GitHub or Jira) resolves itself from the URL — the runner
    # never branches on provider.
    try:
        issue = fetch_issue(
            issue_url, project_name=project_name, project_path=project_path,
        )
    except UnresolvedJiraProjectError as e:
        msg = str(e)
        notify_fn(f"❌ {msg}")
        return False, msg
    except Exception as e:
        return False, f"Failed to fetch issue: {str(e)[:300]}"

    ref = issue.ref
    title = issue.title
    body = issue.body
    comments = issue.comments
    issue_number = ref.key
    label = ref.label
    provider = ref.provider

    # Resolve the GitHub repo that PRs target: the issue's own repo for
    # GitHub, the configured code repo for a Jira-tracked project.
    owner = repo = None
    repo_slug = ref.repo or resolve_code_repository(project_name, project_path)
    if repo_slug and "/" in repo_slug:
        owner, repo = repo_slug.split("/", 1)

    notify_fn(
        f"\U0001f528 Implementing {provider} issue "
        f"{label}{context_label}..."
    )

    # Extract the most recent plan
    plan = _extract_latest_plan(body, comments)
    if not plan:
        return False, (
            f"No plan found in issue {label}. "
            "The issue should contain implementation phases."
        )
    _progress(f"Plan extracted from issue ({len(plan)} chars, {len(comments)} comments)")

    # Plan-review quality gate with autonomous improvement loop
    gate_result = _run_plan_review_gate(
        plan, project_path, notify_fn=notify_fn, issue_url=issue_url,
        project_name=project_name,
    )
    improvement_context = ""
    if isinstance(gate_result, _GateImproved):
        if gate_result.plan != plan and gate_result.issues_fixed:
            improvement_context += (
                "\n\n## Plan Improvement Notes\n\n"
                "The plan was autonomously improved before implementation. "
                "The original plan had these issues that were addressed:\n"
                f"{gate_result.issues_fixed}\n\n"
                "The plan above is the corrected version. Pay attention to the "
                "specific file paths and details added during improvement."
            )
        if gate_result.assumptions_advisory:
            improvement_context += (
                "\n\n## Assumption Verification Required\n\n"
                "A pre-implementation audit flagged these plan assumptions as "
                "unverified and critical. Before writing any code, verify each "
                "one against the actual codebase (Read/Grep). If one turns out "
                "false, adapt the approach and note it in the PR:\n"
                f"{gate_result.assumptions_advisory}"
            )
        plan = gate_result.plan

    # Resolve the effective base branch once; both the implementation prompt
    # and the post-implementation guard need to agree on what counts as
    # "the base branch" for this project (e.g. `staging` on anantys-back).
    from app.projects_config import resolve_base_branch
    effective_base_branch = base_branch or resolve_base_branch(
        project_name or guess_project_name(project_path), project_path,
    )

    # Snapshot the expected feature branch tip before running, so the
    # post-run fallback check can distinguish fresh work from stale branches.
    from app.config import get_branch_prefix
    from app.git_utils import get_commit_subjects as git_commits, run_git
    expected_branch = f"{get_branch_prefix()}implement-{issue_number}"
    rc, pre_run_tip, _ = run_git(
        "rev-parse", "--verify", expected_branch, cwd=project_path,
    )
    pre_run_tip = pre_run_tip.strip() if rc == 0 else None

    # Invoke Claude with the plan
    _progress("Starting implementation with Claude...")
    effective_context = (context or "Implement the full plan.") + improvement_context
    try:
        output = _execute_implementation(
            project_path=project_path,
            issue_url=issue_url,
            issue_title=title,
            plan=plan,
            context=effective_context,
            skill_dir=skill_dir,
            issue_number=str(issue_number),
            project_name=project_name,
            instance_dir=instance_dir,
            base_branch=effective_base_branch,
        )
    except Exception as e:
        return False, f"Implementation failed: {str(e)[:300]}"

    # Detect whether real work landed: commits exist on a feature branch.
    # Claude sometimes checks out the base branch after pushing, so also
    # check the expected feature branch when HEAD is on base.
    # Returns the branch name where work was found, or None.
    def _work_landed() -> Optional[str]:
        branch = get_current_branch(project_path)
        on_base = branch in (effective_base_branch, "main", "master")
        commits = get_commit_subjects(project_path, base_branch=effective_base_branch)
        if bool(commits) and not on_base:
            return branch
        if on_base:
            if git_commits(
                cwd=project_path,
                base_branch=effective_base_branch,
                branch=expected_branch,
            ):
                rc, post_tip, _ = run_git(
                    "rev-parse", "--verify", expected_branch, cwd=project_path,
                )
                post_tip = post_tip.strip() if rc == 0 else None
                if post_tip and post_tip != pre_run_tip:
                    return expected_branch
        return None

    landed_branch = _work_landed() if output else None
    if not output or not landed_branch:
        logger.info(
            "[implement] First pass produced no committed changes — running escalated retry"
        )
        try:
            output = _execute_implementation(
                project_path=project_path,
                issue_url=issue_url,
                issue_title=title,
                plan=plan,
                context=effective_context,
                skill_dir=skill_dir,
                issue_number=str(issue_number),
                project_name=project_name,
                instance_dir=instance_dir,
                base_branch=effective_base_branch,
                escalate=True,
            )
        except Exception as e:
            logger.warning("[implement] Escalated retry failed: %s", e)
            output = ""

        landed_branch = _work_landed() if output else None
        if not output or not landed_branch:
            msg = (
                f"⚠️ /implement could not auto-implement issue {label}{context_label} "
                "after two passes. The plan may need a human review before retrying."
            )
            notify_fn(msg)
            return False, (
                f"No committed changes after two passes for {label}{context_label}."
            )

    # If work landed on a feature branch but HEAD is still on base, check it out
    # so downstream PR submission and notifications see the correct branch.
    current = get_current_branch(project_path)
    if landed_branch and landed_branch != current:
        rc, _, stderr = run_git("checkout", landed_branch, cwd=project_path)
        if rc != 0:
            logger.warning(
                "[implement] Could not checkout %s: %s", landed_branch, stderr,
            )
            notify_fn(
                f"⚠️ Work landed on `{landed_branch}` but checkout failed — "
                "skipping PR submission. A human may need to open the PR manually."
            )
            return True, (
                f"Work landed on {landed_branch} but could not switch to it for PR submission."
            )

    # Post-implementation: submit draft PR (only for GitHub issues with repo info)
    _progress("Implementation complete")
    pr_url = None
    if owner and repo:
        _progress("Submitting draft PR...")
        try:
            pr_url = _submit_implement_pr(
                project_path=project_path,
                owner=owner,
                repo=repo,
                issue_number=str(issue_number),
                issue_title=title,
                issue_url=issue_url,
                skill_dir=skill_dir,
                base_branch=base_branch,
                project_name=project_name,
                notify_fn=notify_fn,
            )
        except (RuntimeError, OSError, ValueError,
                subprocess.SubprocessError) as e:
            logger.warning("PR submission failed: %s", e)
            notify_fn(
                f"\u274c PR submission for issue {label} raised "
                f"{type(e).__name__}: {str(e)[:200]}"
            )

    # Build notification and summary
    branch = get_current_branch(project_path)
    on_base_branch = branch in (effective_base_branch, "main", "master")
    gate_result = run_gate_for_skill(
        project_path=project_path,
        project_name=project_name,
        pr_url=pr_url or "",
        notify_fn=notify_fn,
        skill_origin="implement",
        review_skill_dir=_REVIEW_SKILL_DIR,
        plan_url=issue_url,
    )
    gate_note = format_gate_note(gate_result)
    if pr_url:
        notify_fn(
            f"\u2705 Implementation complete for issue {label}"
            f"{context_label}\nDraft PR: {pr_url}{gate_note}"
        )
        summary = (
            f"Implementation complete for {label}{context_label}"
            f"\nDraft PR: {pr_url}{gate_note}"
        )
    elif not on_base_branch:
        skip_reason = (
            " (PR creation skipped)" if provider != "github"
            else " (PR creation failed \u2014 see prior message for details)"
        )
        notify_fn(
            f"\u2705 Implementation complete for issue {label}"
            f"{context_label}\nBranch: {branch}{skip_reason}"
        )
        summary = (
            f"Implementation complete for {label}{context_label}"
            f"\nBranch: {branch}"
        )
    else:
        notify_fn(
            f"\u26a0\ufe0f Implementation complete for issue {label}"
            f"{context_label} \u2014 changes landed on the base branch "
            f"`{branch}`, no PR created. The skill failed to create a "
            "feature branch; move the commits onto a feature branch "
            "manually before pushing."
        )
        summary = (
            f"Implementation complete for {label}{context_label}"
            f" (on base branch {branch}, no PR)"
        )

    return True, summary


def _is_plan_content(text: str) -> bool:
    """Check if text contains plan structure markers.

    Args:
        text: Text to check for plan markers.

    Returns:
        True if text contains markdown headings indicating a plan structure.
    """
    if not text:
        return False
    return bool(_PLAN_MARKER_RE.search(text))


def _extract_latest_plan(body: Optional[str], comments: List[dict]) -> str:
    """Extract the most recent plan from issue body and comments.

    Multipart Jira plan comments are assembled first.  Other trackers retain
    the existing newest-plan-comment behavior.

    A split Jira plan whose parts are all unclaimable — the authorship check
    refuses every one of them, so nothing is assembled — must not disappear
    quietly: the same fragments are also skipped as standalone plans, so the
    fallback would be the pre-plan issue body presented as the plan. The
    incompleteness banner cannot fire for that case (no part was assembled, so
    nothing reads as missing), hence the explicit warning here.

    Args:
        body: Issue body text.
        comments: List of comment dicts with keys: author, date, body.

    Returns:
        The plan text, or empty string if no plan found.
    """
    multipart_plan, multipart_score = _extract_jira_multipart_plan_scored(comments)
    plan = _select_latest_plan(body, comments, multipart_plan, multipart_score)
    if multipart_plan:
        return plan

    unclaimed = _unclaimable_multipart_fragments(comments)
    if not unclaimed:
        return plan

    logger.warning(
        "%d split Jira plan comment(s) could not be verified as Koan's own — "
        "they were excluded, so this plan may be stale",
        unclaimed,
    )
    if not plan.strip():
        # Nothing else on the issue reads as a plan, so there is no text to
        # carry the warning. Report "no plan" and let the mission fail rather
        # than hand the agent a banner to implement.
        return ""
    return (
        f"> **Warning — a split plan on this issue was ignored.** {unclaimed} "
        f"`Part N of M` comment(s) could not be verified as Koan's own, so the "
        f"text below is whatever else the issue offers and may predate that "
        f"plan. Verify it before implementing.\n\n{plan}"
    )


def _unclaimable_multipart_fragments(comments: List[dict]) -> int:
    """Count comments shaped like a split Jira plan part.

    Only meaningful when assembly returned nothing: every such comment was
    rejected by the authorship check, yet still shadows the plan the agent
    would otherwise run.
    """
    return sum(
        1
        for comment in comments or []
        if (parsed := parse_plan_comment(str(comment.get("body", "") or "")))
        is not None and parsed[2] > 1
    )


def _select_latest_plan(
    body: Optional[str],
    comments: List[dict],
    multipart_plan: str,
    multipart_score: Tuple[str, int],
) -> str:
    """Pick the newest plan text among the assembled group, comments and body."""
    # Check comments from newest to oldest. Never treat an individual Jira
    # multipart fragment as a standalone plan when no group was assembled.
    for index in range(len(comments) - 1, -1, -1):
        comment = comments[index]
        comment_body = comment.get("body", "")
        parsed = parse_plan_comment(comment_body or "")
        if parsed is not None and parsed[2] > 1:
            continue
        if _is_plan_content(comment_body):
            # A leftover group from an older revision must not beat a newer
            # single-part plan: a shrinking plan reuses part 1 and retires the
            # rest, and a retirement Jira accepted but never applied would
            # otherwise strand the agent on the superseded parts.
            single_score = (str(comment.get("updated", "")), index)
            if multipart_plan and multipart_score >= single_score:
                return multipart_plan
            if multipart_plan:
                logger.warning(
                    "Ignoring stale multipart Jira plan — a newer single-part "
                    "plan comment supersedes it",
                )
            return comment_body

    if multipart_plan:
        return multipart_plan

    # Fall back to issue body if it has plan markers
    if _is_plan_content(body):
        return body

    # If no plan markers found, assume the entire body is the plan
    # (allows non-standard plan formats). Body may be None for issues
    # with an empty body — GitHub returns body=null in that case.
    return (body or "").strip()


def _extract_jira_multipart_plan(comments: List[dict]) -> str:
    """Return the newest split Jira plan text, discarding its recency score."""
    return _extract_jira_multipart_plan_scored(comments)[0]


def _extract_jira_multipart_plan_scored(
    comments: List[dict],
) -> Tuple[str, Tuple[str, int]]:
    """Return the newest split Jira plan and its recency score.

    The score is the winning group's newest ``(updated, index)`` pair, so the
    caller can weigh the group against a standalone plan comment instead of
    preferring a multipart group unconditionally.

    Every part of one plan carries the same ``rev`` in its footer, so parts are
    grouped by revision rather than inferred from ordering — the publisher
    updates parts in place, which makes Jira's creation order meaningless as a
    generation order. ``updated`` then picks the newest revision, falling back
    to received order when Jira omits it.

    An incomplete group still yields the parts that are present — a partial plan
    beats refusing to work — but never silently: the footer carries the expected
    count, so a gap is announced in the returned text and the log rather than
    letting the agent implement a truncated plan believing it is whole.

    Only comments Koan is entitled to claim may occupy a part slot. The footer
    is plain text a reviewer reproduces by quoting the tail of a plan, and the
    later comment wins its slot outright — so without the authorship check a
    reviewer's question silently *becomes* part N, and the incompleteness
    banner never fires because nothing is missing, only substituted.
    """
    authored_by_koan = koan_authorship_check(comments)
    groups: dict[str, dict[int, str]] = {}
    group_counts: dict[str, int] = {}
    group_scores: dict[str, tuple[str, int]] = {}
    for index, comment in enumerate(comments):
        if not authored_by_koan(comment):
            continue
        comment_body = str(comment.get("body", "") or "")
        parsed = parse_plan_comment(comment_body)
        if parsed is None:
            continue
        revision, number, count = parsed
        if count < 2 or not 1 <= number <= count:
            continue
        score = (str(comment.get("updated", "")), index)
        groups.setdefault(revision, {})[number] = comment_body
        group_counts[revision] = max(group_counts.get(revision, 0), count)
        group_scores[revision] = max(group_scores.get(revision, ("", -1)), score)

    if not group_scores:
        return "", ("", -1)

    newest = max(group_scores, key=lambda rev: group_scores[rev])
    parts = groups[newest]
    assembled = "\n\n".join(
        strip_plan_envelope(parts[number]).strip() for number in sorted(parts)
    ).strip()

    expected = group_counts[newest]
    missing = [n for n in range(1, expected + 1) if n not in parts]
    if missing:
        gap = ", ".join(str(n) for n in missing)
        logger.warning(
            "Jira plan rev %s is missing part(s) %s of %d — implementing a partial plan",
            newest, gap, expected,
        )
        assembled = (
            f"> **Warning — this plan is incomplete.** Part(s) {gap} of {expected} "
            f"were not found on the issue; the sections below are what is available.\n\n"
            f"{assembled}"
        )
    return assembled, group_scores[newest]


def _plan_hash(plan: str) -> str:
    """SHA-256 hex digest of the plan text (stripped)."""
    return hashlib.sha256(plan.strip().encode()).hexdigest()


def _plan_review_cache_path(project_path: str, project_name: str = "") -> Path:
    """Per-project cache file for the plan-review gate hash."""
    project_name = project_name or guess_project_name(project_path)
    from app.utils import KOAN_ROOT
    return KOAN_ROOT / "instance" / f".plan-review-hash-{project_name}"


def _is_plan_cache_fresh(
    project_path: str, current_hash: str, project_name: str = "",
) -> bool:
    """Return True if the cached plan hash matches — review can be skipped."""
    cache_path = _plan_review_cache_path(project_path, project_name)
    if not cache_path.exists():
        return False
    try:
        return cache_path.read_text().strip() == current_hash
    except OSError:
        return False


def _write_plan_cache(
    project_path: str, plan_hash_hex: str, project_name: str = "",
) -> None:
    """Persist the reviewed plan hash so identical re-runs skip review."""
    try:
        cache_path = _plan_review_cache_path(project_path, project_name)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        from app.utils import atomic_write
        atomic_write(cache_path, plan_hash_hex + "\n")
    except OSError as e:
        logger.warning("Plan-review cache write failed: %s", e)


class _GateImproved:
    """Result when the gate self-healed the plan and/or flagged assumptions.

    Carries the (possibly improved) plan plus an advisory listing unverified
    critical assumptions the implementer must verify before writing code.
    """

    __slots__ = ("plan", "issues_fixed", "assumptions_advisory")

    def __init__(self, plan: str, issues_fixed: str, assumptions_advisory: str = ""):
        self.plan = plan
        self.issues_fixed = issues_fixed
        self.assumptions_advisory = assumptions_advisory


def _run_plan_review_gate(
    plan: str,
    project_path: str,
    notify_fn=None,
    issue_url: str = "",
    project_name: str = "",
) -> Union[None, _GateImproved]:
    """Run plan-review gate with autonomous improvement loop.

    The gate is fail-open by contract: it may improve or annotate the plan,
    but it never blocks implementation on a reviewer verdict.

    Returns:
        None — proceed with original plan (simple/cached/disabled).
        _GateImproved — proceed with improved plan and/or assumptions advisory.
    """
    from app.plan_runner import (
        ASSUMPTIONS_CRITICAL, ASSUMPTIONS_REVIEWER_ERROR,
        improve_plan, is_simple_plan, review_plan, review_plan_assumptions,
    )

    if is_simple_plan(plan):
        logger.debug("Plan is simple — skipping review gate")
        return None

    from app.config import get_plan_review_config

    review_cfg = get_plan_review_config()
    if not review_cfg.get("implement_gate", True):
        return None

    current_hash = _plan_hash(plan)
    if _is_plan_cache_fresh(project_path, current_hash, project_name):
        logger.info("Plan-review gate: cache hit — skipping review")
        return None

    # Assumptions pressure-test: surface unverified critical assumptions so
    # the implementer verifies them first. Advisory only — never blocks.
    assumptions_advisory = ""
    if review_cfg.get("assumptions_check", True):
        logger.info("Plan-review gate: running assumptions check...")
        assumptions_status, assumptions_reason = review_plan_assumptions(
            plan, project_path, _PLAN_SKILL_DIR,
        )
        if assumptions_status == ASSUMPTIONS_CRITICAL:
            assumptions_advisory = assumptions_reason
            logger.warning(
                "Plan-review gate: critical unverified assumption — "
                "proceeding with advisory (fail open)",
            )
            if notify_fn:
                try:
                    notify_fn(
                        "⚠️ Plan assumptions flagged — proceeding; implementer "
                        f"will verify before coding:\n{assumptions_reason}"
                    )
                except Exception:
                    logger.warning("Failed to send assumption-advisory notification", exc_info=True)
        elif assumptions_status == ASSUMPTIONS_REVIEWER_ERROR:
            logger.warning(
                "Plan-review gate: assumptions reviewer error — failing open: %s",
                assumptions_reason,
            )
            if notify_fn:
                try:
                    notify_fn(
                        "⚠️ Assumptions check skipped — reviewer error "
                        f"(proceeding without it):\n{assumptions_reason}"
                    )
                except Exception:
                    logger.warning("Failed to send assumption-fail-open notification", exc_info=True)

    max_rounds = review_cfg.get("max_rounds", 3)
    current_plan = plan
    all_issues: List[str] = []

    for round_num in range(1, max_rounds + 1):
        logger.info("Plan-review gate: round %d/%d...", round_num, max_rounds)
        approved, issues = review_plan(current_plan, project_path, _PLAN_SKILL_DIR)

        if approved:
            logger.info("Plan-review gate: APPROVED (round %d)", round_num)
            final_hash = _plan_hash(current_plan)
            _write_plan_cache(project_path, final_hash, project_name)
            if current_plan != plan:
                _post_improved_plan(current_plan, issue_url, notify_fn)
                return _GateImproved(
                    current_plan, "\n".join(all_issues), assumptions_advisory,
                )
            if assumptions_advisory:
                return _GateImproved(current_plan, "", assumptions_advisory)
            return None

        all_issues.append(issues)
        logger.info(
            "Plan-review gate: ISSUES_FOUND (round %d) — improving...",
            round_num,
        )

        if notify_fn and round_num == 1:
            try:
                notify_fn(
                    f"🔧 Plan review found issues — auto-improving "
                    f"(up to {max_rounds} rounds):\n{issues}"
                )
            except Exception:
                logger.debug("Failed to send improvement notification", exc_info=True)

        if round_num < max_rounds:
            current_plan = improve_plan(
                current_plan, issues, project_path, _PLAN_SKILL_DIR
            )

    # Exhausted all rounds — fail open, use best available plan
    logger.warning(
        "Plan-review gate: exhausted %d rounds — proceeding with best plan (fail open)",
        max_rounds,
    )
    if notify_fn:
        try:
            notify_fn(
                f"⚠️ Plan review couldn't fully resolve issues after {max_rounds} "
                "rounds — proceeding with implementation anyway (fail open)."
            )
        except Exception:
            logger.debug("Failed to send exhaustion notification", exc_info=True)

    if current_plan != plan:
        _post_improved_plan(current_plan, issue_url, notify_fn)
        return _GateImproved(
            current_plan, "\n".join(all_issues), assumptions_advisory,
        )
    if assumptions_advisory:
        return _GateImproved(current_plan, "", assumptions_advisory)
    return None


def _post_improved_plan(
    improved_plan: str, issue_url: str, notify_fn=None,
) -> None:
    """Post the autonomously improved plan as a new comment on the issue."""
    if not issue_url:
        return
    try:
        comment_body = (
            "### 🔧 Plan Improved (auto)\n\n"
            "The plan-review gate found issues and autonomously fixed them. "
            "Proceeding with this improved version:\n\n"
            f"{improved_plan}"
        )
        # The tracker resolves itself from the URL — GitHub or Jira alike.
        add_comment(issue_url, comment_body)
    except Exception:
        logger.debug("Failed to post improved plan to issue tracker", exc_info=True)


def _build_prompt(
    issue_url: str,
    issue_title: str,
    plan: str,
    context: str,
    skill_dir: Optional[Path] = None,
    branch_prefix: str = "koan/",
    issue_number: str = "",
    project_memory: str = "",
    base_branch: str = "main",
    project_path: str = "",
) -> str:
    """Build the implementation prompt from the issue and plan."""
    template_vars = dict(
        ISSUE_URL=issue_url,
        ISSUE_TITLE=issue_title,
        PLAN=plan,
        CONTEXT=context,
        BRANCH_PREFIX=branch_prefix,
        ISSUE_NUMBER=issue_number,
        PROJECT_MEMORY=project_memory,
        BASE_BRANCH=base_branch,
    )

    return load_prompt_or_skill(
        skill_dir, "implement",
        project_path=project_path or None,
        **template_vars,
    )


def _generate_pr_summary(
    project_path: str,
    issue_title: str,
    issue_url: str,
    commit_subjects: List[str],
    skill_dir: Optional[Path] = None,
) -> str:
    """Generate a PR summary using the lightweight model.

    Falls back to a bullet list of commit subjects if the model call
    fails or times out.
    """
    commits_text = "\n".join(f"- {s}" for s in commit_subjects) or "(no commits)"
    fallback = f"Implements {issue_url}\n\n{commits_text}"

    try:
        prompt = load_prompt_or_skill(
            skill_dir, "pr_summary",
            project_path=project_path or None,
            ISSUE_URL=issue_url,
            ISSUE_TITLE=issue_title,
            COMMIT_SUBJECTS=commits_text,
        )

        from app.cli_provider import run_command
        output = run_command(
            prompt, project_path,
            allowed_tools=[],
            model_key="lightweight",
            max_turns=1,
            timeout=300,
            max_turns_source=None,
        )
        return output.strip() if output and output.strip() else fallback
    except Exception as e:
        logger.debug("PR summary generation failed: %s", e)
        return fallback


def _execute_implementation(
    project_path: str,
    issue_url: str,
    issue_title: str,
    plan: str,
    context: str,
    skill_dir: Optional[Path] = None,
    issue_number: str = "",
    project_name: str = "",
    instance_dir: str = "",
    base_branch: Optional[str] = None,
    escalate: bool = False,
) -> str:
    """Execute the implementation via Claude CLI."""
    from app.config import get_branch_prefix
    from app.projects_config import resolve_base_branch
    from app.skill_memory import build_memory_block_for_skill

    branch_prefix = get_branch_prefix()
    effective_base = base_branch or resolve_base_branch(
        project_name or guess_project_name(project_path), project_path,
    )
    project_memory = build_memory_block_for_skill(
        project_path,
        f"{issue_title}\n{plan}",
        project_name=project_name,
        instance_dir=instance_dir,
    )

    effective_context = context
    if escalate:
        escalation_preamble = load_prompt_or_skill(
            skill_dir, "implement_retry_context",
            project_path=project_path or None,
        )
        effective_context = escalation_preamble + "\n\n" + context

    prompt = _build_prompt(
        issue_url, issue_title, plan, effective_context, skill_dir,
        branch_prefix=branch_prefix,
        issue_number=issue_number,
        project_memory=project_memory,
        base_branch=effective_base,
        project_path=project_path,
    )

    from app.claude_step import run_skill_loop
    from app.cli_provider import CLAUDE_TOOLS, run_command_streaming
    from app.config import get_skill_max_turns, get_skill_timeout

    def _step_fn(_evidence):
        return run_command_streaming(
            prompt, project_path,
            allowed_tools=sorted(CLAUDE_TOOLS),
            model_key="mission",
            max_turns=get_skill_max_turns(), timeout=get_skill_timeout(),
        )

    loop_outcome = run_skill_loop(
        step_fn=_step_fn,
        evidence_fn=lambda _a, _r: "",
        should_continue_fn=lambda _a, _r: (False, "done"),
        max_attempts=1,
    )

    attempts = loop_outcome.get("attempts", [])
    if attempts and attempts[0].get("error"):
        raise attempts[0]["error"]
    return attempts[0]["result"] if attempts else ""


# ---------------------------------------------------------------------------
# Post-implementation: draft PR submission (delegates to app.pr_submit)
# ---------------------------------------------------------------------------

def _submit_implement_pr(
    project_path: str,
    owner: str,
    repo: str,
    issue_number: str,
    issue_title: str,
    issue_url: str,
    skill_dir: Optional[Path] = None,
    base_branch: Optional[str] = None,
    project_name: str = "",
    notify_fn=None,
) -> Optional[str]:
    """Build implement-specific PR title/body and delegate to shared submit."""
    from app.pr_submit import get_commit_subjects
    from app.projects_config import resolve_base_branch

    project_name = project_name or guess_project_name(project_path)
    effective_base = base_branch or resolve_base_branch(project_name, project_path)
    commits = get_commit_subjects(project_path, base_branch=effective_base)

    summary = _generate_pr_summary(
        project_path, issue_title, issue_url, commits, skill_dir,
    )

    pr_title = f"Implement: {issue_title}"[:70]
    pr_body = (
        f"## Summary\n\n{summary}\n\n"
        f"Closes {issue_url}\n\n"
        f"---\n{_build_footer()}"
    )

    try:
        from app.describe_pr import describe_pr, format_description
        desc = describe_pr(project_path, effective_base)
        if desc:
            pr_body = (
                f"{format_description(desc)}\n\n"
                f"Closes {issue_url}\n\n"
                f"---\n{_build_footer()}"
            )
    except Exception as e:
        logger.warning("describe_pr failed, using fallback body: %s", e)

    try:
        return submit_draft_pr(
            project_path=project_path,
            project_name=project_name,
            owner=owner,
            repo=repo,
            issue_number=issue_number,
            pr_title=pr_title,
            pr_body=pr_body,
            issue_url=issue_url,
            base_branch=base_branch,
            notify_fn=notify_fn,
            skill_name="implement",
        )
    except Exception as e:
        logger.warning("PR submission failed: %s", e)
        if notify_fn:
            notify_fn(
                f"❌ PR submission raised "
                f"{type(e).__name__}: {str(e)[:200]}"
            )
        return None


# ---------------------------------------------------------------------------
# CLI entry point -- python3 -m app.implement_runner
# ---------------------------------------------------------------------------

def main(argv=None):
    """CLI entry point for implement_runner."""
    import argparse
    from app.url_skill_args import add_url_skill_common_args

    parser = argparse.ArgumentParser(
        description="Implement a plan from a GitHub issue."
    )
    parser.add_argument(
        "--project-path", required=True,
        help="Local path to the project repository",
    )
    parser.add_argument(
        "--issue-url", required=True,
        help="GitHub issue URL containing the plan",
    )
    add_url_skill_common_args(parser)
    cli_args = parser.parse_args(argv)

    skill_dir = Path(__file__).resolve().parent

    success, summary = run_implement(
        project_path=cli_args.project_path,
        issue_url=cli_args.issue_url,
        context=cli_args.context,
        skill_dir=skill_dir,
        base_branch=cli_args.base_branch,
        project_name=cli_args.project_name,
        instance_dir=cli_args.instance_dir,
    )
    print(summary)
    return 0 if success else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
