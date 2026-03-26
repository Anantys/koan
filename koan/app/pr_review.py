#!/usr/bin/env python3
"""
Kōan -- Pull Request review and update workflow.

Multi-step pipeline for /pr command:
1. Fetch PR context from GitHub
2. Checkout and rebase onto target branch
3. Run Claude Code to address review feedback
4. Run refactor pass (if skill available)
5. Run quality review pass (if skill available)
6. Run simplify pass (if refactor skill available — readability-only)
7. Confirm tests pass
8. Force-push and comment on PR
"""

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Optional, Tuple, List

from app.claude_step import (
    _run_git,
    _rebase_onto_target,
    run_claude_step as _run_claude_step,
    run_project_tests,
)
from app.github import run_gh
from app.prompts import load_prompt_or_skill
from app.rebase_pr import fetch_pr_context, _find_remote_for_repo

# Matches skill names like `atoomic.refactor` or my.review (with or without backticks)
_SKILL_RE = re.compile(r'`?([a-zA-Z0-9_-]+\.(?:refactor|review))\b`?')


def build_pr_prompt(context: dict, skill_dir: Path = None) -> str:
    """Build a prompt for Claude to address PR review feedback."""
    return load_prompt_or_skill(
        skill_dir, "pr-review",
        TITLE=context["title"],
        BODY=context["body"],
        BRANCH=context["branch"],
        BASE=context["base"],
        DIFF=context["diff"],
        REVIEW_COMMENTS=context["review_comments"],
        REVIEWS=context["reviews"],
        ISSUE_COMMENTS=context["issue_comments"],
    )


def build_refactor_prompt(project_path: str, skill_name: str = "", skill_dir: Path = None) -> str:
    """Build a prompt for the refactor pass on recent changes."""
    prompt = load_prompt_or_skill(skill_dir, "pr-refactor", PROJECT_PATH=project_path)
    if skill_name:
        prompt += (
            f"\n\n## Skill Invocation\n\n"
            f"Invoke the `{skill_name}` skill using the Skill tool before "
            f"doing any manual refactoring:\n"
            f'`skill: "{skill_name}"`'
        )
    return prompt


def build_simplify_prompt(project_path: str, skill_name: str = "", skill_dir: Path = None) -> str:
    """Build a prompt for the post-review simplify pass (readability-only)."""
    prompt = load_prompt_or_skill(skill_dir, "pr-simplify", PROJECT_PATH=project_path)
    if skill_name:
        prompt += (
            f"\n\n## Skill Invocation\n\n"
            f"Invoke the `{skill_name}` skill in simplify mode using the Skill tool:\n"
            f'`skill: "{skill_name}" --simplify`'
        )
    return prompt


def build_quality_review_prompt(project_path: str, skill_name: str = "", skill_dir: Path = None) -> str:
    """Build a prompt for the quality review pass on recent changes."""
    prompt = load_prompt_or_skill(skill_dir, "pr-quality-review", PROJECT_PATH=project_path)
    if skill_name:
        prompt += (
            f"\n\n## Skill Invocation\n\n"
            f"Invoke the `{skill_name}` skill using the Skill tool before "
            f"doing any manual review:\n"
            f'`skill: "{skill_name}"`'
        )
    return prompt


def detect_test_command(project_path: str) -> Optional[str]:
    """Detect the test command for a project by examining common files.

    Returns:
        Shell command string to run tests, or None if not detected.
    """
    p = Path(project_path)

    # Makefile with 'test' target
    makefile = p / "Makefile"
    if makefile.exists():
        content = makefile.read_text()
        if re.search(r'^test\s*:', content, re.MULTILINE):
            return "make test"

    # Python: pytest
    if (p / "pytest.ini").exists() or (p / "pyproject.toml").exists() or (p / "setup.py").exists():
        # Check if there's a tests directory
        if (p / "tests").is_dir() or (p / "koan" / "tests").is_dir():
            return "make test" if makefile.exists() else "pytest"

    # Node.js
    pkg_json = p / "package.json"
    if pkg_json.exists():
        try:
            pkg = json.loads(pkg_json.read_text())
            if "test" in pkg.get("scripts", {}):
                return "npm test"
        except (json.JSONDecodeError, OSError):
            pass

    return None


def detect_skills(project_path: str = "") -> Tuple[Optional[str], Optional[str]]:
    """Detect available refactor and review skills.

    Searches for skill names matching *.refactor and *.review patterns in:
    1. CLAUDE.md at the project path (closest to runtime skill registry)
    2. soul.md in the instance directory
    3. Falls back to generic "refactor"/"review" if skill mentions exist

    Args:
        project_path: Optional path to project root for CLAUDE.md lookup.

    Returns:
        (refactor_skill, review_skill) — skill names or None if unavailable.
    """
    refactor_skill = None
    review_skill = None

    # Collect text sources to scan for skill references
    sources: List[str] = []

    # 1. Check CLAUDE.md at project path (highest priority — project-specific skills)
    if project_path:
        claude_md = Path(project_path) / "CLAUDE.md"
        if claude_md.exists():
            sources.append(claude_md.read_text())

    # 2. Check soul.md in instance directory
    instance_dir = Path(os.environ.get("KOAN_ROOT", "")) / "instance"
    soul_path = instance_dir / "soul.md"
    if soul_path.exists():
        sources.append(soul_path.read_text())

    # Scan all sources for skill name patterns
    for text in sources:
        if refactor_skill and review_skill:
            break

        for match in _SKILL_RE.finditer(text):
            name = match.group(1)
            if name.endswith(".refactor") and not refactor_skill:
                refactor_skill = name
            elif name.endswith(".review") and not review_skill:
                review_skill = name

        # Fallback: generic mentions of "refactor/review" + "skill"
        if not refactor_skill and "refactor" in text.lower() and "skill" in text.lower():
            refactor_skill = "refactor"
        if not review_skill and "review" in text.lower() and "skill" in text.lower():
            review_skill = "review"

    return refactor_skill, review_skill


def run_pr_review(
    owner: str,
    repo: str,
    pr_number: str,
    project_path: str,
    notify_fn=None,
    skill_dir: Path = None,
) -> Tuple[bool, str]:
    """Execute the full PR review pipeline.

    Steps:
        1. Fetch PR context from GitHub
        2. Checkout the PR branch and rebase onto target branch
        3. Run Claude Code to address review feedback (commit changes)
        4. Run refactor skill if available (separate commit)
        5. Run review skill if available (separate commit)
        6. Run simplify pass if refactor skill available (readability-only, separate commit)
        7. Run tests — fix if broken
        8. Force-push the branch
        9. Comment on PR with summary of all actions

    Args:
        owner: GitHub owner
        repo: GitHub repo name
        pr_number: PR number as string
        project_path: Local path to the project
        notify_fn: Optional callback for progress notifications.
                   Defaults to send_telegram.

    Returns:
        (success, summary) tuple.
    """
    if notify_fn is None:
        from app.notify import send_telegram
        notify_fn = send_telegram

    full_repo = f"{owner}/{repo}"
    actions_log: List[str] = []

    # ── Step 1: Fetch PR context ──────────────────────────────────────
    notify_fn(f"Reading PR #{pr_number}...")
    try:
        context = fetch_pr_context(owner, repo, pr_number)
    except Exception as e:
        return False, f"Failed to fetch PR context: {e}"

    if not context["branch"]:
        return False, "Could not determine PR branch name."

    branch = context["branch"]
    base = context["base"]

    # Determine which local remote corresponds to the PR's target repo
    base_remote = _find_remote_for_repo(owner, repo, project_path)

    # Determine which remote hosts the PR's head branch (the fork)
    head_owner = context.get("head_owner", "")
    head_remote = _find_remote_for_repo(head_owner, repo, project_path) if head_owner else None

    # ── Step 2: Checkout and rebase onto target branch ────────────────
    notify_fn(f"Rebasing `{branch}` onto `{base}`...")
    try:
        _run_git(["git", "fetch", "origin", branch], cwd=project_path)
        _run_git(["git", "checkout", branch], cwd=project_path)
        _run_git(["git", "pull", "origin", branch, "--rebase"], cwd=project_path)
    except Exception as e:
        return False, f"Failed to checkout branch {branch}: {e}"

    # Rebase onto the upstream target branch (prefers the matched remote)
    rebase_remote = _rebase_onto_target(
        base, project_path, preferred_remote=base_remote,
        head_remote=head_remote,
    )
    if rebase_remote:
        actions_log.append(f"Rebased `{branch}` onto `{rebase_remote}/{base}`")
    else:
        return False, f"Rebase conflict on {base} (tried origin and upstream)"

    # ── Step 3: Address review feedback via Claude Code ───────────────
    has_review_feedback = bool(
        context["review_comments"].strip()
        or context["reviews"].strip()
        or context["issue_comments"].strip()
    )

    if has_review_feedback:
        notify_fn(f"Addressing review comments on `{branch}`...")
        _run_claude_step(
            prompt=build_pr_prompt(context, skill_dir=skill_dir),
            project_path=project_path,
            commit_msg=f"pr-review: address feedback on #{pr_number}",
            success_label="Addressed reviewer feedback",
            failure_label="Review feedback step failed",
            actions_log=actions_log,
            max_turns=30,
        )

    # ── Step 4: Refactor pass ─────────────────────────────────────────
    refactor_skill, review_skill = detect_skills(project_path)

    if refactor_skill:
        notify_fn(f"Running refactor pass ({refactor_skill})...")
        _run_claude_step(
            prompt=build_refactor_prompt(project_path, refactor_skill, skill_dir=skill_dir),
            project_path=project_path,
            commit_msg=f"refactor: apply refactoring pass on #{pr_number}",
            success_label=f"Applied refactoring via `{refactor_skill}`",
            failure_label="Refactor step skipped",
            actions_log=actions_log,
            use_skill=True,
        )

    # ── Step 5: Quality review pass ───────────────────────────────────
    if review_skill:
        notify_fn(f"Running quality review pass ({review_skill})...")
        _run_claude_step(
            prompt=build_quality_review_prompt(project_path, review_skill, skill_dir=skill_dir),
            project_path=project_path,
            commit_msg=f"review: apply quality improvements on #{pr_number}",
            success_label=f"Applied quality review via `{review_skill}`",
            failure_label="Quality review step skipped",
            actions_log=actions_log,
            use_skill=True,
        )

    # ── Step 6: Simplify pass (post-review, readability-only) ─────────
    if refactor_skill:
        notify_fn(f"Running simplify pass ({refactor_skill} --simplify)...")
        # If the branch has a single commit, amend it; otherwise add a new commit
        rev_count = subprocess.run(
            ["git", "rev-list", "--count", f"{base}..HEAD"],
            capture_output=True, text=True, cwd=project_path, timeout=30,
        )
        single_commit = rev_count.stdout.strip() == "1"
        _run_claude_step(
            prompt=build_simplify_prompt(project_path, refactor_skill, skill_dir=skill_dir),
            project_path=project_path,
            commit_msg=f"style: simplify readability on #{pr_number}",
            success_label=f"Applied simplify pass via `{refactor_skill} --simplify`",
            failure_label="Simplify step skipped",
            actions_log=actions_log,
            use_skill=True,
            amend=single_commit,
        )

    # ── Step 7: Run tests ──────────────────────────────────────────────
    test_cmd = detect_test_command(project_path)
    if test_cmd:
        notify_fn("Running tests...")
        test_result = run_project_tests(project_path, test_cmd=test_cmd)
        if test_result["passed"]:
            actions_log.append(
                f"Tests passing ({test_result.get('details', 'OK')})"
            )
        else:
            # Try to fix failing tests via Claude
            notify_fn("Tests failing — attempting fix...")
            fix_prompt = (
                f"The test suite is failing after PR changes. "
                f"Test command: `{test_cmd}`\n\n"
                f"Test output:\n```\n{test_result.get('output', '')[:3000]}\n```\n\n"
                f"Fix the failing tests. Only modify what's necessary."
            )
            _run_claude_step(
                prompt=fix_prompt,
                project_path=project_path,
                commit_msg=f"fix: repair tests after PR #{pr_number} changes",
                success_label="",  # handled below via retest
                failure_label="",
                actions_log=[],  # discard — we log based on retest below
                max_turns=15,
                timeout=600,
            )

            # Re-run tests to confirm
            retest = run_project_tests(project_path, test_cmd=test_cmd)
            if retest["passed"]:
                actions_log.append("Tests fixed and passing")
            else:
                actions_log.append(
                    f"Tests still failing: {retest.get('details', 'unknown')}"
                )

    # ── Step 8: Force-push ────────────────────────────────────────────
    notify_fn(f"Pushing `{branch}`...")
    try:
        _run_git(
            ["git", "push", "origin", branch, "--force-with-lease"],
            cwd=project_path,
        )
        actions_log.append(f"Force-pushed `{branch}`")
    except Exception as e:
        return False, f"Push failed: {e}\n\nActions completed before failure:\n" + "\n".join(
            f"- {a}" for a in actions_log
        )

    # ── Step 9: Comment on PR ─────────────────────────────────────────
    comment_body = _build_pr_comment(
        pr_number, branch, base, actions_log, context
    )

    try:
        run_gh(
            "pr", "comment", pr_number,
            "--repo", full_repo,
            "--body", comment_body,
        )
        actions_log.append("Commented on PR")
    except Exception as e:
        # Non-fatal
        notify_fn(f"Changes pushed but failed to comment on PR: {e}")

    summary = f"PR #{pr_number} updated.\n" + "\n".join(
        f"- {a}" for a in actions_log
    )
    return True, summary


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_pr_comment(
    pr_number: str,
    branch: str,
    base: str,
    actions_log: List[str],
    context: dict,
) -> str:
    """Build a markdown-formatted comment for the PR.

    Format:
        ## Description title
        Summary paragraph
        ### Changes
        - bullet list of actions
        ### Status & Next Steps
        Quick message about current state
    """
    # Build a descriptive title from the PR context
    title = context.get("title", f"PR #{pr_number}")

    # Count meaningful changes
    change_count = sum(
        1 for a in actions_log
        if not a.startswith("Tests") and "Force-pushed" not in a
    )

    # Summary paragraph
    if change_count == 0:
        summary = (
            f"Automated pipeline ran on `{branch}`. "
            f"The branch has been rebased on `{base}` and force-pushed."
        )
    else:
        summary = (
            f"Automated pipeline ran {len(actions_log)} steps on `{branch}`: "
            f"rebased onto `{base}`, applied {change_count} change(s), "
            f"and force-pushed the result."
        )

    # Actions as bullet list
    actions_md = "\n".join(f"- {a}" for a in actions_log) if actions_log else "- No changes needed"

    # Status and next steps
    has_failures = any(
        "failing" in a.lower() or "failed" in a.lower() or "skipped" in a.lower()
        for a in actions_log
    )

    if has_failures:
        status_msg = (
            "Some steps encountered issues. "
            "Please review the changes and verify the failing items. "
            "You may want to run the test suite locally before merging."
        )
    else:
        status_msg = (
            "All steps completed successfully. "
            "The branch is rebased, tests are passing, and the PR is ready for re-review."
        )

    return (
        f"## {title}\n\n"
        f"{summary}\n\n"
        f"### Changes\n\n"
        f"{actions_md}\n\n"
        f"### Status & Next Steps\n\n"
        f"{status_msg}\n\n"
        f"---\n"
        f"_Automated by Kōan_"
    )
