"""
Kōan -- CLAUDE.md refresh pipeline.

Analyzes recent commits in a project and updates (or creates) the CLAUDE.md
file with architecturally significant changes.

Usage:
    python -m app.claudemd_refresh <project_path> [--project-name NAME]

Pipeline:
1. Detect whether CLAUDE.md exists in the project
2. Gather git context (commits since last CLAUDE.md modification)
3. Create a feature branch
4. Build a selective prompt focused on architectural changes
5. Invoke Claude CLI to update/create CLAUDE.md
6. Commit, push, and create a draft PR
"""

import argparse
import subprocess
import sys
from pathlib import Path

from app.git_sync import run_git
from app.git_utils import run_git_strict
from app.prompts import load_skill_prompt


def _git_last_modified(project_path: str, filepath: str) -> str:
    """Get the ISO date of the last commit that modified a file.

    Returns empty string if the file has never been committed.
    """
    return run_git(project_path, "log", "-1", "--format=%aI", "--", filepath)


def _git_log_since(project_path: str, since_date: str, max_commits: int = 50) -> str:
    """Get git log since a date, formatted for context."""
    return run_git(
        project_path, "log",
        f"--since={since_date}", f"-n{max_commits}",
        "--format=%h %s", "--no-merges",
    )


def _git_diff_stat_since(project_path: str, since_date: str) -> str:
    """Get diffstat of changes since a date (files changed, not content)."""
    # Find the oldest commit since that date
    log_output = run_git(
        project_path, "log", f"--since={since_date}", "--format=%H", "--reverse",
    )
    commits = log_output.splitlines()
    if not commits:
        return ""
    oldest = commits[0]
    # Check if oldest commit has a parent (fails on root commit)
    has_parent = bool(run_git(project_path, "rev-parse", "--verify", f"{oldest}^"))
    diff_range = f"{oldest}..HEAD" if not has_parent else f"{oldest}~1..HEAD"
    return run_git(project_path, "diff", "--stat", diff_range)


def _git_log_full(project_path: str, max_commits: int = 30) -> str:
    """Get recent git log for INIT mode (no CLAUDE.md exists yet)."""
    return run_git(
        project_path, "log", f"-n{max_commits}", "--format=%h %s", "--no-merges",
    )


def build_git_context(project_path: str, claude_md_exists: bool) -> str:
    """Build the git context section for the prompt."""
    if not claude_md_exists:
        recent = _git_log_full(project_path)
        if recent:
            return f"No CLAUDE.md exists yet. Recent project history:\n\n```\n{recent}\n```"
        return "No CLAUDE.md exists yet. No git history available."

    last_modified = _git_last_modified(project_path, "CLAUDE.md")
    if not last_modified:
        # CLAUDE.md exists but was never committed (maybe .gitignored or new)
        recent = _git_log_full(project_path, max_commits=20)
        return (
            "CLAUDE.md exists but has no git history (uncommitted or new).\n"
            f"Recent commits:\n\n```\n{recent}\n```"
        )

    commits = _git_log_since(project_path, last_modified)
    if not commits:
        return (
            f"CLAUDE.md was last updated: {last_modified}\n\n"
            "No new commits since then. CLAUDE.md is up to date."
        )

    diffstat = _git_diff_stat_since(project_path, last_modified)

    context = f"CLAUDE.md was last updated: {last_modified}\n\n"
    context += f"Commits since then ({len(commits.splitlines())} total):\n\n```\n{commits}\n```"
    if diffstat:
        context += f"\n\nFiles changed (diffstat):\n\n```\n{diffstat}\n```"
    return context


def _has_changes(project_path: str) -> bool:
    """Check if there are uncommitted changes in the working tree."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True, cwd=project_path, timeout=30,
    )
    return bool(result.stdout.strip())


def _create_branch(project_path: str, branch_name: str) -> None:
    """Create and checkout a new branch from the current HEAD."""
    run_git_strict("checkout", "-b", branch_name, cwd=project_path)


def _commit_and_push(project_path: str, branch_name: str, message: str) -> bool:
    """Stage CLAUDE.md, commit, and push. Returns True if a commit was created."""
    if not _has_changes(project_path):
        return False
    run_git_strict("add", "CLAUDE.md", cwd=project_path)
    run_git_strict("commit", "-m", message, cwd=project_path)
    run_git_strict("push", "-u", "origin", branch_name, cwd=project_path)
    return True


def _create_pr(
    project_path: str,
    project_name: str,
    mode: str,
    base_branch: str,
) -> str:
    """Create a draft PR for the CLAUDE.md update. Returns the PR URL."""
    from app.github import pr_create, resolve_target_repo

    if mode == "INIT":
        title = f"docs: create CLAUDE.md for {project_name}"
        body = (
            "## What\n"
            f"Create initial CLAUDE.md for **{project_name}**.\n\n"
            "## Why\n"
            "No CLAUDE.md existed — this bootstraps the reference document "
            "for AI coding assistants working on this project.\n\n"
            "---\n_Generated by Kōan `/claudemd`_"
        )
    else:
        title = f"docs: update CLAUDE.md for {project_name}"
        body = (
            "## What\n"
            f"Update CLAUDE.md for **{project_name}** to reflect recent changes.\n\n"
            "## Why\n"
            "Architecturally significant changes landed since the last update. "
            "This keeps the AI reference document in sync with the codebase.\n\n"
            "---\n_Generated by Kōan `/claudemd`_"
        )

    pr_kwargs = {
        "title": title,
        "body": body,
        "draft": True,
        "base": base_branch,
        "cwd": project_path,
    }

    # Target upstream repo when working in a fork
    upstream = resolve_target_repo(project_path)
    if upstream:
        pr_kwargs["repo"] = upstream
        try:
            from app.pr_submit import get_fork_owner
            fork_owner = get_fork_owner(project_path)
            if fork_owner:
                from app.git_utils import get_current_branch
                branch = get_current_branch(cwd=project_path)
                pr_kwargs["head"] = f"{fork_owner}:{branch}"
        except Exception as exc:
            import logging
            logging.getLogger(__name__).debug("Fork owner detection failed: %s", exc)

    return pr_create(**pr_kwargs)


def run_refresh(project_path: str, project_name: str) -> int:
    """Run the CLAUDE.md refresh pipeline.

    Returns:
        0 on success, 1 on failure.
    """
    from app.claude_step import run_claude
    from app.cli_provider import build_full_command
    from app.config import get_branch_prefix, get_model_config

    project_path = str(Path(project_path).resolve())
    claude_md = Path(project_path) / "CLAUDE.md"
    claude_md_exists = claude_md.exists()

    # Determine mode
    if claude_md_exists:
        mode = "UPDATE"
        mode_instructions = (
            "CLAUDE.md already exists. Analyze the recent commits to find "
            "architecturally significant changes and update the file accordingly. "
            "Make minimal, surgical edits. If nothing significant changed, say so."
        )
    else:
        mode = "INIT"
        mode_instructions = (
            "No CLAUDE.md exists yet. Explore the project structure, build system, "
            "and codebase to create a comprehensive but concise CLAUDE.md from scratch. "
            "Focus on: what the project does, how to build/test/run it, key architecture, "
            "and important conventions."
        )

    # Gather git context (before branching, so we see main's history)
    git_context = build_git_context(project_path, claude_md_exists)

    # Check if there's nothing to do
    if claude_md_exists and "No new commits since then" in git_context:
        print("CLAUDE.md is up to date — no new commits since last update.")
        return 0

    # Remember the base branch for the PR
    base_branch = run_git_strict(
        "rev-parse", "--abbrev-ref", "HEAD", cwd=project_path,
    ).strip()

    # Create a feature branch
    prefix = get_branch_prefix()
    branch_name = f"{prefix}update-claudemd-{project_name}"
    try:
        _create_branch(project_path, branch_name)
    except (RuntimeError, subprocess.SubprocessError) as e:
        print(f"Failed to create branch {branch_name}: {e}", file=sys.stderr)
        return 1

    # Build prompt
    skill_dir = Path(__file__).parent.parent / "skills" / "core" / "claudemd"
    prompt = load_skill_prompt(
        skill_dir, "refresh-claude-md",
        MODE=mode,
        MODE_INSTRUCTIONS=mode_instructions,
        PROJECT_PATH=project_path,
        PROJECT_NAME=project_name,
        GIT_CONTEXT=git_context,
    )

    # Build CLI command
    models = get_model_config()
    cmd = build_full_command(
        prompt=prompt,
        allowed_tools=["Bash", "Read", "Write", "Edit", "Glob", "Grep"],
        model=models.get("mission", ""),
        fallback=models.get("fallback", ""),
        max_turns=10,
    )

    # Run Claude to edit CLAUDE.md
    result = run_claude(cmd, project_path, timeout=300)

    if not result["success"]:
        # Return to base branch on failure
        run_git_strict("checkout", base_branch, cwd=project_path)
        print(f"CLAUDE.md refresh failed: {result['error']}", file=sys.stderr)
        return 1

    print(result["output"])

    # Commit, push, and create PR
    commit_msg = (
        f"docs: {'create' if mode == 'INIT' else 'update'} CLAUDE.md"
    )
    try:
        committed = _commit_and_push(project_path, branch_name, commit_msg)
    except (RuntimeError, subprocess.SubprocessError) as e:
        run_git_strict("checkout", base_branch, cwd=project_path)
        print(f"Failed to commit/push: {e}", file=sys.stderr)
        return 1

    if not committed:
        # Claude decided no changes were needed
        run_git_strict("checkout", base_branch, cwd=project_path)
        print("No changes to CLAUDE.md — nothing to commit.")
        return 0

    try:
        pr_url = _create_pr(project_path, project_name, mode, base_branch)
        print(f"PR created: {pr_url.strip()}")
    except (RuntimeError, subprocess.SubprocessError) as e:
        print(f"Branch pushed but PR creation failed: {e}", file=sys.stderr)

    # Return to the base branch
    run_git_strict("checkout", base_branch, cwd=project_path)
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Refresh or create CLAUDE.md for a project",
    )
    parser.add_argument("project_path", help="Path to the project directory")
    parser.add_argument(
        "--project-name", default="",
        help="Project name (defaults to directory basename)",
    )
    args = parser.parse_args()

    project_name = args.project_name or Path(args.project_path).name

    sys.exit(run_refresh(args.project_path, project_name))


if __name__ == "__main__":
    main()
