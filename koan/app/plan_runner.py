"""
Kōan -- Plan runner.

Generates structured implementation plans and posts them as GitHub issues
or comments. Extracted from the /plan skill handler so it can run as a
queued mission via run.py instead of inline in the bridge process.

Issue-centric workflow:
  - New idea: search for existing plan issue first, update if found, create if not
  - Existing issue URL: read all comments, produce updated plan, post as comment
  - Always reuse existing issues to keep the conversation in one place

CLI:
    python3 -m app.plan_runner --project-path <path> --idea "Add dark mode"
    python3 -m app.plan_runner --project-path <path> --issue-url <url>
    python3 -m app.plan_runner --project-path <path> --issue-url <url> --base-branch main
"""

import logging
import re
import sys
from contextlib import suppress
from pathlib import Path
from typing import Optional, Tuple

import app.messaging_level as _messaging
from app.issue_tracker import (
    UnresolvedJiraProjectError,
    add_comment,
    create_issue,
    fetch_issue,
    find_existing_plan_issue,
    project_name_for_path,
    resolve_issue_ref,
    tracker_is_configured,
    tracker_provider,
    tracker_supports_labels,
)
from app.prompts import load_prompt_or_skill
from app.tracker_comment_format import (
    build_plan_comment_failure,
    build_plan_comment_success,
)
from app.url_skill_args import merge_context_with_base_branch

logger = logging.getLogger(__name__)

# Label used to tag plan issues for searchability
_PLAN_LABEL = "plan"


def run_plan(
    project_path: str,
    idea: Optional[str] = None,
    issue_url: Optional[str] = None,
    notify_fn=None,
    skill_dir: Optional[Path] = None,
    context: Optional[str] = None,
    base_branch: Optional[str] = None,
    project_name: str = "",
    instance_dir: str = "",
    iterations: int = 1,
) -> Tuple[bool, str]:
    """Execute the plan pipeline.

    Either generates a new plan (idea mode) or iterates on an existing
    issue (issue_url mode). Exactly one of idea or issue_url must be set.

    Args:
        context: Optional additional user context (e.g. "Focus on phase 2").
                 Appended to the issue context or passed to plan generation.

    Returns:
        (success, summary) tuple.
    """
    # Progress lines are gated behind messaging.level=debug when we default the
    # sink; the outcome line always reaches chat via _messaging.notify_outcome().
    if notify_fn is None:
        from app.messaging_level import progress_notify
        notify_fn = progress_notify(log_category="plan")

    # Heartbeat so the liveness watchdog in run.py knows we're alive
    # before Claude CLI starts streaming.
    print("[plan] Starting plan runner", flush=True)

    if issue_url:
        return _run_issue_plan(
            project_path, issue_url, notify_fn, skill_dir, context=context,
            base_branch=base_branch,
            project_name=project_name, instance_dir=instance_dir,
            iterations=iterations,
        )
    elif idea:
        return _run_new_plan(
            project_path, idea, notify_fn, skill_dir, context=context,
            base_branch=base_branch,
            project_name=project_name, instance_dir=instance_dir,
            iterations=iterations,
        )
    else:
        return False, "No idea or issue URL provided."


def _run_new_plan(
    project_path: str,
    idea: str,
    notify_fn,
    skill_dir: Optional[Path],
    context: Optional[str] = None,
    base_branch: Optional[str] = None,
    project_name: str = "",
    instance_dir: str = "",
    iterations: int = 1,
) -> Tuple[bool, str]:
    """Generate a plan for a new idea, reusing an existing issue if found."""
    notify_fn(f"\U0001f9e0 Planning: {idea[:100]}{'...' if len(idea) > 100 else ''}")
    print(f"[plan] New plan for: {idea[:80]}", flush=True)

    project_name = project_name or project_name_for_path(project_path)

    existing = find_existing_plan_issue(project_name, project_path, idea)
    if existing:
        notify_fn(
            f"\U0001f504 Found existing {existing.provider} issue "
            f"{existing.label} — iterating"
        )
        return _run_issue_plan(
            project_path, existing.url, notify_fn, skill_dir, context=context,
            base_branch=base_branch,
            project_name=project_name, instance_dir=instance_dir,
            iterations=iterations,
        )

    effective_context = merge_context_with_base_branch(context, base_branch)

    print("[plan] Invoking Claude for plan generation", flush=True)
    try:
        plan = _generate_plan(
            project_path, idea, context=effective_context, skill_dir=skill_dir,
            project_name=project_name, instance_dir=instance_dir,
            iterations=iterations, notify_fn=notify_fn,
        )
    except Exception as e:
        return False, f"Plan generation failed: {str(e)[:300]}"

    if not plan:
        return False, "Claude returned an empty plan."

    title = _extract_title(plan)
    plan_body = _strip_title_line(plan)
    from app.pr_footer import build_koan_footer
    issue_body = f"{plan_body}\n\n---\n{build_koan_footer()}"

    if not tracker_is_configured(project_name, project_path):
        _messaging.notify_outcome(f"✅ Plan generated inline:\n\n{plan[:3000]}", notify_fn)
        return True, "Plan generated inline (no issue tracker configured)."

    provider = tracker_provider(project_name, project_path)

    labels = [_PLAN_LABEL] if tracker_supports_labels(project_name, project_path) else None
    try:
        result_url = create_issue(
            project_name, project_path, title, issue_body, labels=labels,
        )
    except (RuntimeError, OSError):
        # GitHub labels may not exist; Jira ignores them. Retry without labels.
        try:
            result_url = create_issue(project_name, project_path, title, issue_body)
        except (RuntimeError, OSError) as e2:
            _messaging.notify_outcome(
                f"⚠️ Plan ready but tracker issue creation failed "
                f"({e2}):\n\n{plan[:3000]}",
                notify_fn,
            )
            return True, f"Plan generated but issue creation failed: {e2}"

    _messaging.notify_outcome(f"✅ Plan created: {result_url}", notify_fn)
    return True, f"Plan created: {result_url}"


def _deliver_jira_plan(
    issue_url: str,
    instance_dir: str,
    notify_fn,
    comment_body: Optional[str] = None,
) -> Tuple[bool, str]:
    """Stage (when given a body) and publish the Jira plan comment.

    Jira's write endpoints report success for writes that never became a
    visible comment, so delivery is confirmed by reading the comment back.
    Returns ``(posted, detail)`` — the Jira comment id on success, a failure
    reason otherwise. The failure notification is emitted here so the resumed
    and freshly-generated paths report identically.
    """
    from app.jira_plan_publish import publish_staged_plan, stage_path_for, stage_plan

    if comment_body is not None:
        stage_plan(issue_url, comment_body, instance_dir)

    posted, detail = publish_staged_plan(issue_url, instance_dir)
    if posted:
        return True, detail

    stage = stage_path_for(issue_url, instance_dir)
    if detail == "stage_clear_failed":
        # The comment is live; only the local artifact survived. Saying Jira
        # rejected the plan here would send the reader hunting the wrong fault.
        summary = f"Plan posted, but its staged copy could not be removed: {stage}"
        _messaging.notify_outcome(
            f"⚠️ {summary}. Until it is, /plan on this issue republishes the "
            "same plan instead of generating a new one.",
            notify_fn,
        )
        return False, summary

    if detail.startswith("abandoned"):
        note = "the staged plan was dropped, so the next /plan regenerates it"
    else:
        note = f"the plan stays staged for retry at {stage}"
    summary = f"Jira could not verify the plan comment: {detail}"
    _messaging.notify_outcome(
        f"❌ Jira did not confirm the plan comment ({detail}); {note}.", notify_fn,
    )
    return False, summary


def _run_issue_plan(
    project_path: str,
    issue_url: str,
    notify_fn,
    skill_dir: Optional[Path],
    context: Optional[str] = None,
    base_branch: Optional[str] = None,
    project_name: str = "",
    instance_dir: str = "",
    iterations: int = 1,
) -> Tuple[bool, str]:
    """Read an existing issue/PR + comments, generate updated plan, post comment."""
    project_name = project_name or project_name_for_path(project_path)

    # Resolve the reference first (no network) for a useful heartbeat and to
    # validate the URL; the tracker then handles fetch/comment generically.
    try:
        ref = resolve_issue_ref(
            issue_url, project_name=project_name, project_path=project_path,
        )
    except UnresolvedJiraProjectError as e:
        msg = str(e)
        _messaging.notify_outcome(f"❌ {msg}", notify_fn)
        return False, msg
    except Exception as e:
        return False, f"Failed to fetch issue: {str(e)[:300]}"

    notify_fn(f"\U0001f4d6 Reading {ref.provider} issue {ref.label}...")
    print(f"[plan] Fetching tracker issue {issue_url}", flush=True)

    # A prior Jira publish failure already has a generated plan on disk.  Try
    # delivery before spending another model run generating an equivalent plan.
    if ref.provider == "jira":
        from app.jira_plan_publish import load_staged_plan

        if load_staged_plan(issue_url, instance_dir) is not None:
            posted, detail = _deliver_jira_plan(issue_url, instance_dir, notify_fn)
            if not posted:
                return False, detail
            _messaging.notify_outcome(
                f"✅ Plan posted as comment on {ref.label} (Jira comment {detail}): {issue_url}",
                notify_fn,
            )
            return True, f"Plan posted on {ref.label}: {issue_url}"

    try:
        content = fetch_issue(
            issue_url, project_name=project_name, project_path=project_path,
        )
    except UnresolvedJiraProjectError as e:
        msg = str(e)
        _messaging.notify_outcome(f"❌ {msg}", notify_fn)
        return False, msg
    except Exception as e:
        return False, f"Failed to fetch issue: {str(e)[:300]}"

    title = content.title
    body = content.body
    comments_text = _format_comments(content.comments)
    label = content.ref.label

    print("[plan] Issue fetched, building prompt", flush=True)
    # Build full issue context for the iteration prompt
    context_parts = [f"## Original Issue {label}: {title}\n\n{body}"]
    if comments_text:
        context_parts.append(f"\n\n## Discussion Comments\n\n{comments_text}")
    else:
        context_parts.append("\n\n*No comments yet on this issue.*")
    effective_context = merge_context_with_base_branch(context, base_branch)
    if effective_context:
        context_parts.append(f"\n\n## User Instructions\n\n{effective_context}")
    issue_context = "\n".join(context_parts)

    print("[plan] Invoking Claude for plan generation", flush=True)
    try:
        plan = _generate_iteration_plan(
            project_path, issue_context, skill_dir=skill_dir,
            project_name=project_name, instance_dir=instance_dir,
            iterations=iterations, notify_fn=notify_fn,
        )
    except Exception as e:
        reason = f"Plan generation failed: {str(e)[:300]}"
        if ref.provider == "jira":
            with suppress(Exception):
                add_comment(
                    issue_url,
                    build_plan_comment_failure("jira", reason),
                    project_name=project_name,
                    project_path=project_path,
                )
        return False, reason

    if not plan:
        reason = "Claude returned an empty plan."
        if ref.provider == "jira":
            with suppress(Exception):
                add_comment(
                    issue_url,
                    build_plan_comment_failure("jira", reason),
                    project_name=project_name,
                    project_path=project_path,
                )
        return False, reason

    iteration_title = _extract_title(plan)
    plan_body = _strip_title_line(plan)
    comment_body = build_plan_comment_success(
        ref.provider, iteration_title, plan_body,
    )

    if ref.provider == "jira":
        posted, detail = _deliver_jira_plan(
            issue_url, instance_dir, notify_fn, comment_body=comment_body,
        )
        if not posted:
            return False, detail
        label = f"{label} (Jira comment {detail})"
    else:
        try:
            posted = add_comment(
                issue_url, comment_body,
                project_name=project_name,
                project_path=project_path,
            )
            if not posted:
                raise RuntimeError("tracker declined the comment")
        except Exception as e:
            _messaging.notify_outcome(f"Plan ready but comment failed ({e}):\n\n{plan[:3000]}", notify_fn)
            return False, f"Plan generated but comment failed: {e}"

    if title:
        label = f"{label} ({title[:60]})"
    _messaging.notify_outcome(f"✅ Plan posted as comment on {label}: {issue_url}", notify_fn)
    return True, f"Plan posted on {label}: {issue_url}"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# Minimum phase count and line count below which review is skipped.
# Single-phase plans under this line threshold are considered trivially simple.
_REVIEW_SKIP_PHASES = 2   # skip if fewer than this many phases
_REVIEW_SKIP_LINES = 20   # skip if plan body is shorter than this


def is_simple_plan(plan_text: str) -> bool:
    """Return True if the plan is trivially simple and doesn't need review.

    Skips review for single-phase plans with fewer than _REVIEW_SKIP_LINES
    lines — e.g. "rename function X to Y" doesn't need a subagent review.
    """
    phase_count = len(re.findall(r'^#{1,4}\s*Phase\b', plan_text, re.MULTILINE | re.IGNORECASE))
    if phase_count >= _REVIEW_SKIP_PHASES:
        return False
    line_count = len([l for l in plan_text.splitlines() if l.strip()])
    return line_count < _REVIEW_SKIP_LINES


def review_plan(plan_text: str, project_path: str, skill_dir) -> Tuple[bool, str]:
    """Run a lightweight subagent to review plan quality.

    Args:
        plan_text: The generated plan text to review.
        project_path: Project directory (for run_command cwd).
        skill_dir: Skill directory for loading the review prompt.

    Returns:
        (approved, issues) tuple:
          - approved=True, issues="" when APPROVED
          - approved=False, issues=<bullet list> when ISSUES_FOUND
          - approved=True, issues="" on reviewer error (fail open)
    """
    from app.cli_provider import run_command

    try:
        prompt = load_prompt_or_skill(
            skill_dir, "plan-review", project_path=project_path, PLAN=plan_text,
        )
    except Exception as e:
        print(f"[plan_runner] Review prompt load failed: {e}", file=sys.stderr)
        return True, ""

    try:
        output = run_command(
            prompt, project_path,
            allowed_tools=["Read", "Glob", "Grep"],
            model_key="lightweight",
            max_turns=3,
            timeout=120,
            max_turns_source=None,
        )
    except Exception as e:
        print(f"[plan_runner] Review subagent failed: {e} — skipping review", file=sys.stderr)
        return True, ""

    if not output:
        return True, ""

    first_line = output.strip().splitlines()[0].strip() if output.strip() else ""

    if first_line.upper().startswith("APPROVED"):
        return True, ""

    if first_line.upper().startswith("ISSUES_FOUND"):
        # Everything after the first line is the list of issues
        rest = "\n".join(output.strip().splitlines()[1:]).strip()
        return False, rest

    # Malformed reviewer output — fail open rather than block
    print(
        f"[plan_runner] Review returned unexpected output (treating as approved): "
        f"{first_line!r}",
        file=sys.stderr,
    )
    return True, ""


ASSUMPTIONS_OK = "ok"
ASSUMPTIONS_CRITICAL = "critical_assumption"
ASSUMPTIONS_REVIEWER_ERROR = "reviewer_error"


def review_plan_assumptions(
    plan_text: str, project_path: str, skill_dir,
) -> Tuple[str, str]:
    """Run a lightweight subagent to pressure-test plan assumptions.

    Args:
        plan_text: The plan to audit for hidden assumptions.
        project_path: Project directory (for run_command cwd).
        skill_dir: Skill directory for loading the assumptions prompt.

    Returns:
        (status, reason) tuple where status is one of:
          - ASSUMPTIONS_OK: assumptions are safe (reason="").
          - ASSUMPTIONS_CRITICAL: a critical assumption is unverified.
          - ASSUMPTIONS_REVIEWER_ERROR: reviewer infrastructure failed. The
            return only signals the error; the caller decides open vs. closed.
            Both current consumers (_apply_assumptions_audit here and
            /implement's _run_plan_review_gate) fail open — the audit is
            advisory and never blocks.
    """
    from app.cli_provider import run_command

    try:
        prompt = load_prompt_or_skill(
            skill_dir, "plan-assumptions", project_path=project_path, PLAN=plan_text,
        )
    except (FileNotFoundError, OSError) as e:
        logger.warning("Assumptions prompt load failed: %s", e)
        return ASSUMPTIONS_REVIEWER_ERROR, f"assumptions prompt load failed: {e}"

    try:
        output = run_command(
            prompt, project_path,
            allowed_tools=["Read", "Glob", "Grep"],
            model_key="lightweight",
            max_turns=3,
            timeout=120,
            max_turns_source=None,
        )
    except Exception as e:
        logger.warning("Assumptions subagent failed: %s", e)
        return ASSUMPTIONS_REVIEWER_ERROR, f"assumptions subagent failed: {e}"

    if not output:
        logger.warning("Assumptions subagent returned empty output")
        return ASSUMPTIONS_REVIEWER_ERROR, "assumptions check produced empty output — manual review needed"

    first_line = output.strip().splitlines()[0].strip() if output.strip() else ""

    if first_line.upper().startswith("ASSUMPTIONS_OK"):
        return ASSUMPTIONS_OK, ""

    if first_line.upper().startswith("CRITICAL_ASSUMPTION_UNVERIFIED"):
        rest = "\n".join(output.strip().splitlines()[1:]).strip()
        return ASSUMPTIONS_CRITICAL, rest

    logger.warning(
        "Assumptions check returned unparseable output: %r",
        first_line,
    )
    return ASSUMPTIONS_REVIEWER_ERROR, "assumptions check produced unparseable output — manual review needed"


# Heading of the section the plan template mandates for genuine unknowns
# (see system-prompts/_partials/plan-tail-sections.md).
_OPEN_QUESTIONS_RE = re.compile(r"^#{2,4}\s+open questions\s*$", re.IGNORECASE | re.MULTILINE)

_ASSUMPTIONS_AUDIT_HEADER = (
    "**Assumptions audit (auto):** the following plan assumptions were flagged "
    "as unverified — confirm or embed evidence before implementing:"
)


def _merge_assumptions_into_open_questions(plan_text: str, findings: str) -> str:
    """Fold assumptions-audit findings into the plan's Open Questions section.

    Appends the findings at the end of the Open Questions section (i.e. just
    before the next same-or-higher-level heading), or appends a new section at
    the end of the plan when none exists. Pure text transform — no LLM round.
    """
    block = f"{_ASSUMPTIONS_AUDIT_HEADER}\n\n{findings.strip()}"

    match = _OPEN_QUESTIONS_RE.search(plan_text)
    if not match:
        return f"{plan_text.rstrip()}\n\n### Open Questions\n\n{block}\n"

    heading_level = match.group(0).strip().split(" ", 1)[0]  # "##", "###" or "####"
    next_heading = re.compile(
        rf"^#{{2,{len(heading_level)}}}\s+", re.MULTILINE,
    )
    section_start = match.end()
    next_match = next_heading.search(plan_text, section_start)
    insert_at = next_match.start() if next_match else len(plan_text)

    before = plan_text[:insert_at].rstrip()
    after = plan_text[insert_at:]
    return f"{before}\n\n{block}\n\n{after.lstrip()}" if after.strip() else f"{before}\n\n{block}\n"


def _apply_assumptions_audit(
    plan_text: str,
    project_path: str,
    skill_dir,
    notify_fn=None,
    project_name: str = "",
) -> str:
    """Pressure-test the final plan's assumptions before it is posted.

    Advisory and fail-open: unverified critical assumptions are folded into
    the plan's Open Questions section so the human can resolve them on the
    tracker before /implement; any auditor error leaves the plan unchanged.
    """
    from app.config import get_plan_review_config

    if is_simple_plan(plan_text):
        return plan_text
    if not get_plan_review_config().get("assumptions_check", True):
        return plan_text

    status, reason = review_plan_assumptions(plan_text, project_path, skill_dir)
    if status == ASSUMPTIONS_CRITICAL:
        logger.info("Assumptions audit flagged unverified assumptions — adding to Open Questions")
        if notify_fn:
            with suppress(Exception):
                notify_fn(
                    "🔍 Assumptions audit flagged unverified assumptions — "
                    "added to the plan's Open Questions"
                )
        return _merge_assumptions_into_open_questions(plan_text, reason)

    if status == ASSUMPTIONS_REVIEWER_ERROR:
        logger.warning("Assumptions audit skipped (reviewer error): %s", reason)
    return plan_text


def improve_plan(
    plan_text: str, issues_text: str, project_path: str, skill_dir
) -> str:
    """Run a codebase-grounded subagent to fix plan quality issues.

    The improver explores the codebase to resolve ambiguities identified by
    the reviewer (missing file paths, vague descriptions, etc.) and returns
    a corrected plan. Uses mission model (not lightweight) because it needs
    full reasoning to fix structural plan issues, not just spot-check reviews.

    Args:
        plan_text: The plan that failed review.
        issues_text: Bullet list of issues from the reviewer.
        project_path: Project directory (for codebase exploration).
        skill_dir: Skill directory for loading the improve prompt.

    Returns:
        Improved plan text, or original plan_text on failure.
    """
    from app.cli_provider import run_command

    try:
        prompt = load_prompt_or_skill(
            skill_dir, "plan-improve",
            project_path=project_path, PLAN=plan_text, ISSUES=issues_text,
        )
    except Exception as e:
        print(f"[plan_runner] Improve prompt load failed: {e}", file=sys.stderr)
        return plan_text

    try:
        output = run_command(
            prompt, project_path,
            allowed_tools=["Read", "Glob", "Grep"],
            model_key="mission",
            max_turns=5,
            timeout=180,
            max_turns_source=None,
        )
    except Exception as e:
        print(f"[plan_runner] Improve subagent failed: {e}", file=sys.stderr)
        return plan_text

    if not output or not output.strip():
        print("[plan_runner] Improve subagent returned empty — keeping original", file=sys.stderr)
        return plan_text

    return output.strip()


def _critic_loop(
    plan_text: str,
    project_path: str,
    idea: str,
    context: str,
    skill_dir,
    iterations: int,
    notify_fn=None,
    is_iteration: bool = False,
    issue_context: str = "",
    project_name: str = "",
    instance_dir: str = "",
) -> str:
    """Refine a plan through N-1 rounds of critique + regeneration.

    Turn 1 is the initial generation (already done by caller).
    Turns 2..N each run: critic -> regenerate with feedback.
    """
    from app.cli_provider import run_command
    from app.config import get_skill_timeout
    from app.skill_memory import build_memory_block_for_skill

    current_plan = plan_text
    _noop_notify = lambda msg: None
    notify = notify_fn or _noop_notify

    notify(f"\U0001f504 Planning... (turn 1/{iterations})")

    for turn in range(2, iterations + 1):
        print(f"[plan_runner] Critic turn {turn}/{iterations}: invoking critic", file=sys.stderr)
        try:
            critic_prompt = load_prompt_or_skill(
                skill_dir, "plan-critic",
                project_path=project_path,
                PLAN=current_plan,
                IDEA=idea or issue_context[:500],
            )
        except Exception as e:
            print(f"[plan_runner] Critic prompt load failed: {e}", file=sys.stderr)
            break

        try:
            critique = run_command(
                critic_prompt, project_path,
                allowed_tools=["Read", "Glob", "Grep"],
                model_key="lightweight",
                max_turns=3,
                timeout=min(120, get_skill_timeout()),
            )
        except Exception as e:
            print(f"[plan_runner] Critic failed: {e} — keeping current plan", file=sys.stderr)
            notify(f"⚠️ Critic round {turn} failed — posting best plan so far")
            break

        if not critique or not critique.strip():
            print(f"[plan_runner] Critic turn {turn}: empty response — treating as failure", file=sys.stderr)
            notify(f"⚠️ Critic round {turn} returned empty — posting best plan so far")
            break

        if "NO_GAPS_FOUND" in critique:
            print(f"[plan_runner] Critic turn {turn}: no gaps found — done early", file=sys.stderr)
            break

        print(f"[plan_runner] Critic turn {turn}: gaps found, regenerating", file=sys.stderr)

        feedback_section = f"\n\n## Critic Feedback (turn {turn})\n\n{critique}"
        try:
            project_memory = build_memory_block_for_skill(
                project_path,
                issue_context if is_iteration else idea,
                project_name=project_name,
                instance_dir=instance_dir,
            )
            if is_iteration:
                new_plan = _run_claude_plan(
                    load_prompt_or_skill(
                        skill_dir, "plan-iterate",
                        project_path=project_path,
                        ISSUE_CONTEXT=issue_context + feedback_section,
                        PROJECT_MEMORY=project_memory,
                    ),
                    project_path,
                    project_name=project_name,
                )
            else:
                feedback_context = (context or "") + feedback_section
                new_plan = _run_claude_plan(
                    load_prompt_or_skill(
                        skill_dir, "plan",
                        project_path=project_path,
                        IDEA=idea, CONTEXT=feedback_context,
                        PROJECT_MEMORY=project_memory,
                    ),
                    project_path,
                    project_name=project_name,
                )
        except Exception as e:
            print(f"[plan_runner] Regeneration failed: {e} — keeping current plan", file=sys.stderr)
            notify(f"⚠️ Regeneration round {turn} failed — posting best plan so far")
            break

        if new_plan:
            current_plan = new_plan
        else:
            print("[plan_runner] Regeneration returned empty — keeping current plan", file=sys.stderr)

        notify(f"\U0001f504 Planning... (turn {turn}/{iterations})")

    return current_plan


def _review_loop(
    plan_text: str,
    project_path: str,
    idea: str,
    context: str,
    skill_dir,
    max_rounds: int = 3,
    is_iteration: bool = False,
    issue_context: str = "",
    project_name: str = "",
    instance_dir: str = "",
) -> str:
    """Iteratively review and re-generate a plan until approved or rounds exhausted.

    Args:
        plan_text: Initial plan text to review.
        project_path: Project directory.
        idea: Original idea string (for new plans).
        context: User context string (for new plans).
        skill_dir: Skill directory.
        max_rounds: Maximum review+regen cycles.
        is_iteration: If True, re-generate using plan-iterate prompt.
        issue_context: Issue context string (for iteration plans).

    Returns:
        Final plan text (best version after review loop).
    """
    current_plan = plan_text
    prev_issues: Optional[str] = None
    final_round = 0

    for round_num in range(1, max_rounds + 1):
        approved, issues = review_plan(current_plan, project_path, skill_dir)
        final_round = round_num

        if approved:
            print(f"[plan_runner] Review round {round_num}: APPROVED", file=sys.stderr)
            _record_plan_metric(project_path, True, round_num, "", project_name)
            return current_plan

        print(f"[plan_runner] Review round {round_num}: ISSUES_FOUND", file=sys.stderr)
        if issues:
            print(f"[plan_runner] Issues:\n{issues}", file=sys.stderr)

        if round_num == max_rounds:
            print(
                f"[plan_runner] Max review rounds ({max_rounds}) exhausted — "
                "posting best version with warning",
                file=sys.stderr,
            )
            _record_plan_metric(
                project_path, False, round_num, issues or "", project_name,
            )
            return current_plan + _review_warning_note(issues, max_rounds)

        # Note if the same issues recur
        if prev_issues and prev_issues == issues:
            print(
                "[plan_runner] Same issues persisted from previous round — "
                "regenerating with stronger context",
                file=sys.stderr,
            )
        prev_issues = issues

        # Re-generate with reviewer feedback appended
        feedback_context = (context or "") + f"\n\n## Review Feedback\n\n{issues}"
        try:
            from app.skill_memory import build_memory_block_for_skill
            if is_iteration:
                project_memory = build_memory_block_for_skill(
                    project_path,
                    issue_context,
                    project_name=project_name,
                    instance_dir=instance_dir,
                )
                new_plan = _run_claude_plan(
                    load_prompt_or_skill(
                        skill_dir, "plan-iterate",
                        project_path=project_path,
                        ISSUE_CONTEXT=issue_context + f"\n\n## Review Feedback\n\n{issues}",
                        PROJECT_MEMORY=project_memory,
                    ),
                    project_path,
                    project_name=project_name,
                )
            else:
                project_memory = build_memory_block_for_skill(
                    project_path,
                    idea,
                    project_name=project_name,
                    instance_dir=instance_dir,
                )
                new_plan = _run_claude_plan(
                    load_prompt_or_skill(
                        skill_dir, "plan",
                        project_path=project_path,
                        IDEA=idea, CONTEXT=feedback_context,
                        PROJECT_MEMORY=project_memory,
                    ),
                    project_path,
                    project_name=project_name,
                )
        except Exception as e:
            print(f"[plan_runner] Re-generation failed: {e} — keeping previous plan", file=sys.stderr)
            _record_plan_metric(
                project_path, False, final_round, "re-generation failed",
                project_name,
            )
            return current_plan

        if new_plan:
            current_plan = new_plan
        else:
            print("[plan_runner] Re-generation returned empty — keeping previous plan", file=sys.stderr)

    _record_plan_metric(
        project_path, False, final_round, "loop exhausted", project_name,
    )
    return current_plan


def _record_plan_metric(
    project_path: str,
    approved: bool,
    rounds: int,
    issues_summary: str,
    project_name: str = "",
) -> None:
    """Record a plan-review metric (fire-and-forget)."""
    try:
        import os
        instance_dir = os.path.join(os.environ.get("KOAN_ROOT", ""), "instance")
        project_name = project_name or project_name_for_path(project_path)
        from app.skill_metrics import record_plan_metric
        record_plan_metric(instance_dir, project_name, approved, rounds, issues_summary)
    except Exception as e:
        print(f"[plan_runner] Failed to record plan metric: {e}", file=sys.stderr)


def _review_warning_note(issues: str, max_rounds: int) -> str:
    """Build the warning note appended to a plan when review rounds are exhausted."""
    return (
        f"\n\n> ⚠️ Plan review flagged unresolved items after {max_rounds} rounds "
        f"— human review recommended.\n>\n"
        + "\n".join(f"> - {line.removeprefix('- ')}" for line in issues.splitlines() if line.strip())
    )


def _generate_plan(
    project_path,
    idea,
    context="",
    skill_dir=None,
    project_name: str = "",
    instance_dir: str = "",
    iterations: int = 1,
    notify_fn=None,
):
    """Run Claude to generate a structured plan for a new idea."""
    from app.config import get_plan_review_config
    from app.skill_memory import build_memory_block_for_skill

    project_memory = build_memory_block_for_skill(
        project_path, idea, project_name=project_name, instance_dir=instance_dir,
    )
    prompt = load_prompt_or_skill(
        skill_dir, "plan", project_path=project_path,
        IDEA=idea, CONTEXT=context, PROJECT_MEMORY=project_memory,
    )
    plan = _run_claude_plan(prompt, project_path, project_name=project_name)

    if iterations > 1:
        plan = _critic_loop(
            plan, project_path, idea=idea, context=context,
            skill_dir=skill_dir, iterations=iterations, notify_fn=notify_fn,
            project_name=project_name, instance_dir=instance_dir,
        )

    review_cfg = get_plan_review_config()
    if review_cfg["enabled"] and not is_simple_plan(plan):
        plan = _review_loop(
            plan, project_path, idea=idea, context=context, skill_dir=skill_dir,
            max_rounds=review_cfg["max_rounds"],
            project_name=project_name, instance_dir=instance_dir,
        )

    return _apply_assumptions_audit(
        plan, project_path, skill_dir, notify_fn=notify_fn,
        project_name=project_name,
    )


def _generate_iteration_plan(
    project_path,
    issue_context,
    skill_dir=None,
    project_name: str = "",
    instance_dir: str = "",
    iterations: int = 1,
    notify_fn=None,
):
    """Run Claude to generate an updated plan based on issue + comments."""
    from app.config import get_plan_review_config
    from app.skill_memory import build_memory_block_for_skill

    project_memory = build_memory_block_for_skill(
        project_path,
        issue_context,
        project_name=project_name,
        instance_dir=instance_dir,
    )
    prompt = load_prompt_or_skill(
        skill_dir, "plan-iterate",
        project_path=project_path,
        ISSUE_CONTEXT=issue_context,
        PROJECT_MEMORY=project_memory,
    )
    plan = _run_claude_plan(prompt, project_path, project_name=project_name)

    if iterations > 1:
        plan = _critic_loop(
            plan, project_path, idea="", context="",
            skill_dir=skill_dir, iterations=iterations, notify_fn=notify_fn,
            is_iteration=True, issue_context=issue_context,
            project_name=project_name, instance_dir=instance_dir,
        )

    review_cfg = get_plan_review_config()
    if review_cfg["enabled"] and not is_simple_plan(plan):
        plan = _review_loop(
            plan, project_path, idea="", context="", skill_dir=skill_dir,
            max_rounds=review_cfg["max_rounds"],
            is_iteration=True, issue_context=issue_context,
            project_name=project_name, instance_dir=instance_dir,
        )

    return _apply_assumptions_audit(
        plan, project_path, skill_dir, notify_fn=notify_fn,
        project_name=project_name,
    )


# Regex matching preamble transition lines — everything up to and including
# such a line is stripped from the CLI output so it doesn't pollute the
# GitHub issue body.
_PREAMBLE_RE = re.compile(
    r"(?:now I have (?:all )?the context|"
    r"let me create the (?:comprehensive |structured )?plan|"
    r"here(?:'s| is) the (?:comprehensive |structured |implementation )?plan|"
    r"I'll create the plan|"
    r"let me (?:now )?(?:generate|write|draft|produce) the plan)",
    re.IGNORECASE,
)


def _strip_preamble(output: str) -> str:
    """Strip CLI exploration noise from the beginning of plan output.

    Some CLI providers (notably Copilot) include tool-call output and
    thinking text before the actual plan content. This function finds the
    last preamble transition line and returns only the plan that follows.
    """
    if not output:
        return output

    lines = output.splitlines()

    # Find the last line that matches a known preamble pattern.
    # Everything up to and including that line is noise.
    last_preamble_idx = -1
    for i, line in enumerate(lines):
        if _PREAMBLE_RE.search(line):
            last_preamble_idx = i

    if last_preamble_idx >= 0:
        remaining = "\n".join(lines[last_preamble_idx + 1:]).strip()
        if remaining:
            return remaining

    return output


def _is_error_output(output: str) -> bool:
    """Detect CLI error patterns in output that should not be posted as a plan."""
    if not output:
        return False
    # Claude CLI emits this when --max-turns is exhausted
    if "Reached max turns" in output:
        return True
    # Other known error patterns
    if output.lstrip().startswith("Error:") and len(output) < 200:
        return True
    return False


def _run_claude_plan(prompt, project_path, project_name: str = ""):
    """Execute the CLI plan generation and return the output.

    Loads project MCP servers when the ``plan`` role is opted in via
    ``mcp_roles`` (default includes plan). Secondary planning sub-agents
    (critic/improve/review/assumptions) use ``run_command`` directly and get no
    MCP — they do not go through this helper.

    They are *intended* to be read-only, but note that passing a narrow
    ``allowed_tools`` list does not by itself make them so: ``--allowedTools``
    pre-approves, it does not withhold. Enforcement only applies when the role
    resolves into :data:`app.provider.READ_ONLY_ROLES`, and these sub-agents run
    as ``lightweight`` or ``mission`` — so the allowlist withholds nothing.
    """
    from app.cli_provider import run_command_streaming
    from app.config import (
        MCP_ROLE_PLAN,
        get_skill_max_turns,
        get_skill_timeout,
        mcp_configs_for_role,
    )
    output = run_command_streaming(
        prompt, project_path,
        allowed_tools=["Read", "Glob", "Grep", "WebFetch"],
        model_key="mission",
        max_turns=get_skill_max_turns(), timeout=get_skill_timeout(),
        project_name=project_name,
        mcp_configs=mcp_configs_for_role(MCP_ROLE_PLAN, project_name),
    )
    if _is_error_output(output):
        raise RuntimeError(output)
    return _strip_preamble(output)






def _format_comments(comments):
    """Format comments list into readable text with authorship.

    Args:
        comments: List of dicts with keys: author, date, body.
    """
    if not isinstance(comments, list) or not comments:
        return ""

    parts = []
    for c in comments:
        author = c.get("author", "unknown")
        date = c.get("date", "")[:10]
        body = c.get("body", "").strip()
        if body:
            parts.append(f"**{author}** ({date}):\n{body}")
    return "\n\n---\n\n".join(parts)



def _extract_title(plan_text):
    """Extract the title from the first non-empty line of the plan.

    The plan prompt instructs Claude to write a short, descriptive title
    as the very first line (no # prefix). This function extracts that line,
    stripping any accidental markdown formatting or CLI noise characters.
    """
    lines = plan_text.strip().splitlines()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # Strip any markdown heading prefix the model may have added
        clean = re.sub(r'^#+\s*', '', line).strip()
        # Strip CLI noise characters (bullets, arrows, decorative prefixes)
        clean = re.sub(r'^[●•►▸▹▶◆◇○◎▪▫→⟶»>]+\s*', '', clean).strip()
        # Skip generic section headings that aren't real titles
        if clean.lower() in (
            "summary", "implementation plan", "plan",
            "open questions", "implementation steps",
            "implementation phases",
        ):
            continue
        if clean:
            return clean[:120]
    return "Implementation Plan"


def _strip_title_line(plan_text):
    """Remove the first non-empty line (title) from the plan text.

    The title is used as the GitHub issue title, so we strip it from the
    body to avoid duplication.
    """
    lines = plan_text.strip().splitlines()
    # Find and skip the first non-empty line
    for i, line in enumerate(lines):
        if line.strip():
            remaining = "\n".join(lines[i + 1:]).strip()
            return remaining if remaining else plan_text
    return plan_text


def _extract_idea_from_issue(body):
    """Extract the core idea from an issue body for re-planning."""
    if not body:
        return "Review and update this plan"
    lines = body.strip().splitlines()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("---") or line.startswith("*Generated by"):
            continue
        clean = re.sub(r'^#+\s*', '', line).strip()
        clean = re.sub(r'^Plan:\s*', '', clean).strip()
        if clean and len(clean) > 3:
            return clean[:500]
    return "Review and refine this plan based on the discussion"


# ---------------------------------------------------------------------------
# CLI entry point -- python3 -m app.plan_runner
# ---------------------------------------------------------------------------

def main(argv=None):
    """CLI entry point for plan_runner.

    Returns exit code (0 = success, 1 = failure).
    """
    import argparse
    from app.url_skill_args import add_url_skill_common_args

    parser = argparse.ArgumentParser(
        description="Generate a structured plan and post as GitHub issue/comment."
    )
    parser.add_argument(
        "--project-path", required=True,
        help="Local path to the project repository",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--idea",
        help="New idea to plan",
    )
    group.add_argument(
        "--issue-url",
        help="GitHub issue URL to iterate on",
    )
    add_url_skill_common_args(parser)
    parser.add_argument(
        "--iterations", type=int, default=1, choices=range(1, 6),
        help="Number of critique+refine turns (1-5, default 1)",
    )
    cli_args = parser.parse_args(argv)

    skill_dir = Path(__file__).resolve().parent.parent / "skills" / "core" / "plan"

    success, summary = run_plan(
        project_path=cli_args.project_path,
        idea=cli_args.idea,
        issue_url=cli_args.issue_url,
        skill_dir=skill_dir,
        context=cli_args.context,
        base_branch=cli_args.base_branch,
        project_name=cli_args.project_name,
        instance_dir=cli_args.instance_dir,
        iterations=cli_args.iterations,
    )
    print(summary)
    return 0 if success else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
