"""
Kōan -- Deep plan runner.

Spec-first design pipeline: explores 2-3 approaches via Socratic questioning,
runs a spec review loop (up to 5 iterations), posts the approved spec to the
configured issue tracker, and queues a follow-up /plan mission for human approval.

CLI:
    python3 -m skills.core.deepplan.deepplan_runner \
        --project-path <path> --idea "Refactor auth middleware"
"""

import sys
from pathlib import Path
from typing import Optional, Tuple

from app.issue_tracker import (
    create_issue,
    fetch_issue,
    project_name_for_path,
    tracker_is_configured,
    tracker_supports_labels,
)
from app.prompts import load_prompt_or_skill
from app.url_skill_args import merge_context_with_base_branch

# Maximum spec review iterations before posting best-effort result
_MAX_REVIEW_ROUNDS = 5

# Label for deepplan issues
_DEEPPLAN_LABEL = "deepplan"


def run_deepplan(
    project_path: str,
    idea: str,
    notify_fn=None,
    skill_dir: Optional[Path] = None,
    issue_url: Optional[str] = None,
    context: Optional[str] = None,
    base_branch: Optional[str] = None,
    project_name: str = "",
) -> Tuple[bool, str]:
    """Execute the deep plan pipeline.

    1. Explore 2-3 design approaches via Claude.
    2. Run spec review loop (up to 5 iterations) using a lightweight model.
    3. Post approved spec to the configured issue tracker.
    4. Queue a /plan <issue-url> follow-up mission.
    5. Notify via Telegram.

    Args:
        project_path: Local path to the project repository.
        idea: Idea or feature to design (free text).
        notify_fn: Optional notification function.
        skill_dir: Optional skill directory for prompt loading.
        issue_url: Optional issue URL to fetch context from.
            When provided, the issue title/body/comments are fetched
            and used to enrich the idea context for exploration.
        context: Optional additional user instructions.
        base_branch: Optional base-branch hint for downstream planning.
        project_name: Optional resolved project name from dispatcher.

    Returns:
        (success, summary) tuple.
    """
    if notify_fn is None:
        from app.notify import send_telegram
        notify_fn = send_telegram

    project_name = project_name or project_name_for_path(project_path)
    user_context = merge_context_with_base_branch(context, base_branch)

    # When issue_url is provided, fetch issue context and enrich the idea
    issue_context = ""
    if issue_url:
        idea, issue_context = _enrich_idea_from_issue(
            idea,
            issue_url,
            notify_fn,
            project_name=project_name,
            project_path=project_path,
        )
        if user_context:
            issue_context = (
                f"{issue_context}\n\n## User Instructions\n\n{user_context}"
                if issue_context
                else f"## User Instructions\n\n{user_context}"
            )
    elif user_context:
        issue_context = f"## User Instructions\n\n{user_context}"

    notify_fn(f"\U0001f9e0 Deep planning: {idea[:100]}{'...' if len(idea) > 100 else ''}")

    if not tracker_is_configured(project_name, project_path):
        return False, "No issue tracker configured for this project."

    # Phase 1: Explore design approaches
    try:
        spec = _explore_design(project_path, idea, skill_dir, issue_context=issue_context)
    except Exception as e:
        return False, f"Design exploration failed: {str(e)[:300]}"

    if not spec:
        return False, "Claude returned an empty spec."

    # Phase 2: Spec review loop
    spec = _review_loop(spec, project_path, idea, skill_dir, notify_fn)

    # Phase 3: Post spec as a tracker issue
    title = _extract_title(spec)
    spec_body = _strip_title_line(spec)
    from app.pr_footer import build_koan_footer
    issue_body = f"{spec_body}\n\n---\n{build_koan_footer()}"

    labels = [_DEEPPLAN_LABEL] if tracker_supports_labels(project_name, project_path) else None
    try:
        issue_url = create_issue(
            project_name, project_path, title, issue_body, labels=labels,
        )
    except (RuntimeError, OSError):
        try:
            issue_url = create_issue(project_name, project_path, title, issue_body)
        except (RuntimeError, OSError) as e:
            notify_fn(
                f"\u26a0\ufe0f Spec ready but issue creation failed "
                f"({e}):\n\n{spec[:3000]}"
            )
            return True, f"Spec generated but issue creation failed: {e}"

    issue_url = issue_url.strip()
    notify_fn(f"\u2705 Spec posted: {issue_url}")

    # Phase 4: Queue /plan <issue-url> follow-up mission
    _queue_plan_mission(project_path, issue_url)

    summary = f"Spec posted: {issue_url} — /plan queued for approval"
    notify_fn(f"\U0001f4cb Follow-up /plan queued. Review spec then trigger: /plan {issue_url}")
    return True, summary


def _enrich_idea_from_issue(
    idea,
    issue_url,
    notify_fn,
    *,
    project_name: str = "",
    project_path: str = "",
):
    """Fetch issue context from the configured tracker and enrich the idea.

    Args:
        idea: Current idea text (may be just the URL).
        issue_url: Issue URL.
        notify_fn: Notification function.

    Returns:
        Tuple of (enriched_idea, issue_context_text).
    """
    notify_fn("\U0001f50d Fetching issue context from tracker...")

    try:
        issue = fetch_issue(
            issue_url,
            project_name=project_name or None,
            project_path=project_path or None,
        )
        title = issue.title
        body = issue.body
        comments = issue.comments
    except Exception as e:
        # Falling back to an empty context is deliberate here, but it must not
        # be invisible: `idea` is often just the issue URL, so the spec that
        # follows is built from a bare link. Say so instead of quietly
        # producing a thin spec the user cannot account for.
        print(f"[deepplan_runner] Failed to fetch issue: {e}", file=sys.stderr)
        notify_fn(
            f"⚠️ Could not fetch issue context ({str(e)[:200]}) — "
            "continuing from the text supplied only."
        )
        return idea, ""

    # Build the enriched idea from issue content
    enriched = title or idea
    issue_parts = []
    if body:
        issue_parts.append(f"## Issue Description\n\n{body}")
    if comments:
        formatted = _format_issue_comments(comments)
        if formatted:
            issue_parts.append(f"## Discussion\n\n{formatted}")

    issue_context = "\n\n".join(issue_parts)

    # If the original idea was just the URL, use the issue title as the idea
    if idea.strip() == issue_url.strip():
        enriched_idea = title if title else idea
    else:
        enriched_idea = idea

    return enriched_idea, issue_context


def _format_issue_comments(comments):
    """Format issue comments into readable text."""
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


def _explore_design(project_path, idea, skill_dir=None, issue_context=""):
    """Run Claude to explore 2-3 design approaches for the idea."""
    prompt = load_prompt_or_skill(
        skill_dir, "deepplan-explore",
        IDEA=idea,
        ISSUE_CONTEXT=issue_context,
    )

    from app.cli_provider import run_command
    from app.config import get_analysis_max_turns, get_skill_timeout
    output = run_command(
        prompt, project_path,
        allowed_tools=["Read", "Glob", "Grep", "WebFetch"],
        max_turns=get_analysis_max_turns(), timeout=get_skill_timeout(),
    )
    return output


def _review_spec(spec_text, project_path, skill_dir):
    """Run a lightweight subagent to review spec quality.

    Returns:
        (approved, issues) tuple:
          - approved=True, issues="" when APPROVED
          - approved=False, issues=<reason> when ISSUES_FOUND
          - approved=True, issues="" on reviewer error (fail open)
    """
    from app.cli_provider import run_command

    try:
        prompt = load_prompt_or_skill(skill_dir, "deepplan-review", SPEC=spec_text)
    except Exception as e:
        print(f"[deepplan_runner] Review prompt load failed: {e}", file=sys.stderr)
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
        print(
            f"[deepplan_runner] Review subagent failed: {e} — skipping review",
            file=sys.stderr,
        )
        return True, ""

    if not output:
        return True, ""

    first_line = output.strip().splitlines()[0].strip() if output.strip() else ""

    if first_line.upper().startswith("APPROVED"):
        return True, ""

    if first_line.upper().startswith("ISSUES_FOUND"):
        rest = "\n".join(output.strip().splitlines()[1:]).strip()
        return False, rest

    # Malformed reviewer output — fail open
    print(
        f"[deepplan_runner] Review returned unexpected output (treating as approved): "
        f"{first_line!r}",
        file=sys.stderr,
    )
    return True, ""


def _review_loop(spec_text, project_path, idea, skill_dir, notify_fn):
    """Iteratively review and re-explore until approved or rounds exhausted.

    Returns:
        Final spec text (best version after review loop).
    """
    current_spec = spec_text

    for round_num in range(1, _MAX_REVIEW_ROUNDS + 1):
        approved, issues = _review_spec(current_spec, project_path, skill_dir)

        if approved:
            print(f"[deepplan_runner] Review round {round_num}: APPROVED", file=sys.stderr)
            return current_spec

        print(f"[deepplan_runner] Review round {round_num}: ISSUES_FOUND", file=sys.stderr)
        if issues:
            print(f"[deepplan_runner] Issues:\n{issues}", file=sys.stderr)

        if round_num == _MAX_REVIEW_ROUNDS:
            print(
                f"[deepplan_runner] Max review rounds ({_MAX_REVIEW_ROUNDS}) exhausted — "
                "posting best version",
                file=sys.stderr,
            )
            return current_spec

        # Re-explore with reviewer feedback
        feedback_idea = f"{idea}\n\n## Review Feedback\n\n{issues}"
        try:
            new_spec = _explore_design(project_path, feedback_idea, skill_dir)
        except Exception as e:
            print(
                f"[deepplan_runner] Re-exploration failed: {e} — keeping previous spec",
                file=sys.stderr,
            )
            return current_spec

        if new_spec:
            current_spec = new_spec
        else:
            print(
                "[deepplan_runner] Re-exploration returned empty — keeping previous spec",
                file=sys.stderr,
            )

    return current_spec


def _queue_plan_mission(project_path, issue_url):
    """Queue a /plan <issue-url> follow-up mission."""
    try:
        from app.utils import get_known_projects, project_name_for_path, insert_pending_mission
        import os

        koan_root = os.environ.get("KOAN_ROOT", "")
        if not koan_root:
            return

        missions_path = Path(koan_root) / "instance" / "missions.md"
        if not missions_path.exists():
            return

        project_label = project_name_for_path(project_path)
        mission_entry = f"- [project:{project_label}] /plan {issue_url}"
        insert_pending_mission(missions_path, mission_entry)
    except Exception as e:
        print(f"[deepplan_runner] Failed to queue /plan mission: {e}", file=sys.stderr)


def _extract_title(spec_text):
    """Extract the title from the first non-empty line of the spec."""
    import re
    lines = spec_text.strip().splitlines()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # Strip markdown heading prefix
        clean = re.sub(r'^#+\s*', '', line).strip()
        # Strip CLI noise characters
        clean = re.sub(r'^[●•►▸▹▶◆◇○◎▪▫→⟶»>]+\s*', '', clean).strip()
        if clean.lower() in ("summary", "design spec", "spec", "overview"):
            continue
        if clean:
            return clean[:120]
    return "Design Spec"


def _strip_title_line(spec_text):
    """Remove the first non-empty line (title) from the spec text."""
    lines = spec_text.strip().splitlines()
    for i, line in enumerate(lines):
        if line.strip():
            remaining = "\n".join(lines[i + 1:]).strip()
            return remaining if remaining else spec_text
    return spec_text


# ---------------------------------------------------------------------------
# CLI entry point -- python3 -m skills.core.deepplan.deepplan_runner
# ---------------------------------------------------------------------------

def main(argv=None):
    """CLI entry point for deepplan_runner."""
    import argparse
    from app.url_skill_args import add_url_skill_common_args

    parser = argparse.ArgumentParser(
        description="Spec-first design: explore approaches, review, post spec as tracker issue."
    )
    parser.add_argument(
        "--project-path", required=True,
        help="Local path to the project repository",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--idea",
        help="Idea or feature to design",
    )
    group.add_argument(
        "--issue-url",
        help="Issue URL to use as context for the design exploration",
    )
    # --project-path, --idea, and --issue-url are declared above; the shared
    # helper adds --context, --base-branch, --project-name, and --instance-dir.
    # Keep these sets disjoint — adding an overlapping flag here would make
    # argparse raise a conflict at runtime.
    add_url_skill_common_args(parser)
    cli_args = parser.parse_args(argv)

    skill_dir = Path(__file__).resolve().parent

    idea = cli_args.idea or cli_args.issue_url
    success, summary = run_deepplan(
        project_path=cli_args.project_path,
        idea=idea,
        issue_url=cli_args.issue_url,
        context=cli_args.context,
        base_branch=cli_args.base_branch,
        project_name=cli_args.project_name,
        skill_dir=skill_dir,
    )
    print(summary)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
