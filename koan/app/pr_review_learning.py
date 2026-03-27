"""Kōan — PR review learning for autonomous alignment.

Extracts actionable lessons from human PR reviews (comments, approvals,
rejections, closures) and persists them to the project's learnings.md.

The PR feedback system (pr_feedback.py) tracks *merge velocity* — how fast
PRs get merged by category. This module goes deeper: it reads the actual
review comments and actions to learn *what* the human values, critiques,
or rejects.

Architecture:
1. Fetch: GitHub API via gh CLI (review comments, states, closed PRs)
2. Analyze: Claude CLI (lightweight model) parses raw feedback into lessons
3. Persist: New lessons are appended to memory/projects/{name}/learnings.md

The learnings.md file is already consumed by deep_research.py,
prompt_builder.py, and format_outbox.py — so lessons written here
are automatically surfaced to the agent without additional wiring.
"""

import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional


def fetch_pr_reviews(
    project_path: str,
    days: int = 30,
    limit: int = 30,
) -> List[dict]:
    """Fetch recent koan/* PRs with their review data.

    For each PR, fetches:
    - Basic info (number, title, state, branch)
    - Reviews (state, body, author)
    - Review comments (body, path)

    Args:
        project_path: Path to the git repo.
        days: Look back this many days.
        limit: Maximum PRs to fetch.

    Returns:
        List of enriched PR dicts with review data.
    """
    try:
        from app.github import run_gh
    except ImportError:
        return []

    try:
        from app.config import get_branch_prefix
        prefix = get_branch_prefix()
    except Exception as e:
        print(f"[pr_review_learning] branch prefix lookup failed: {e}", file=sys.stderr)
        prefix = "koan/"

    # Fetch all non-open PRs in a single call to avoid double-fetching
    try:
        raw = run_gh(
            "pr", "list",
            "--state", "all",
            "--limit", str(limit),
            "--json", "number,title,createdAt,mergedAt,closedAt,headRefName,state",
            cwd=project_path,
            timeout=15,
        )
        prs = json.loads(raw)
    except Exception as e:
        print(f"[pr_review_learning] Failed to fetch PRs: {e}", file=sys.stderr)
        prs = []

    # Filter to koan/* branches, non-open, and recent PRs
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    filtered = []
    for pr in prs:
        # Skip open PRs — only merged/closed have review learnings
        state = (pr.get("state") or "").upper()
        if state == "OPEN":
            continue

        branch = pr.get("headRefName", "")
        if not branch.startswith(prefix):
            continue

        # Check date (merged or closed)
        date_str = pr.get("mergedAt") or pr.get("closedAt") or pr.get("createdAt", "")
        pr_date = _parse_iso(date_str)
        if pr_date and pr_date < cutoff:
            continue

        filtered.append(pr)

    # Enrich each PR with reviews and comments
    enriched = []
    for pr in filtered[:limit]:
        num = pr["number"]
        reviews = _fetch_reviews_for_pr(project_path, num)
        comments = _fetch_review_comments_for_pr(project_path, num)

        pr["reviews"] = reviews
        pr["review_comments"] = comments
        pr["was_merged"] = bool(pr.get("mergedAt"))
        enriched.append(pr)

    return enriched


def _fetch_reviews_for_pr(project_path: str, pr_number: int) -> List[dict]:
    """Fetch review submissions for a single PR."""
    try:
        from app.github import run_gh
        raw = run_gh(
            "api",
            f"repos/{{owner}}/{{repo}}/pulls/{pr_number}/reviews",
            "--jq", ".[].{state: .state, body: .body, user: .user.login}",
            cwd=project_path,
            timeout=10,
        )
        if not raw.strip():
            return []
        # gh --jq outputs one JSON object per line
        reviews = []
        for line in raw.strip().split("\n"):
            line = line.strip()
            if line:
                try:
                    reviews.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return reviews
    except Exception as e:
        print(f"[pr_review_learning] Reviews fetch failed for #{pr_number}: {e}",
              file=sys.stderr)
        return []


def _fetch_review_comments_for_pr(project_path: str, pr_number: int) -> List[dict]:
    """Fetch inline review comments for a single PR."""
    try:
        from app.github import run_gh
        raw = run_gh(
            "api",
            f"repos/{{owner}}/{{repo}}/pulls/{pr_number}/comments",
            "--jq", ".[].{body: .body, path: .path, user: .user.login}",
            cwd=project_path,
            timeout=10,
        )
        if not raw.strip():
            return []
        comments = []
        for line in raw.strip().split("\n"):
            line = line.strip()
            if line:
                try:
                    comments.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return comments
    except Exception as e:
        print(f"[pr_review_learning] Comments fetch failed for #{pr_number}: {e}",
              file=sys.stderr)
        return []


def format_reviews_for_analysis(prs: List[dict]) -> str:
    """Format enriched PR data as text for Claude to analyze.

    Produces a structured summary of each PR with its reviews and comments,
    suitable as input to the analysis prompt.

    Args:
        prs: List of enriched PR dicts from fetch_pr_reviews().

    Returns:
        Formatted text string, or empty string if no reviews to analyze.
    """
    if not prs:
        return ""

    sections = []
    for pr in prs:
        status = "MERGED" if pr.get("was_merged") else "CLOSED (not merged)"
        header = f"## PR #{pr['number']}: {pr.get('title', '')} [{status}]"
        lines = [header]

        for review in pr.get("reviews", []):
            body = (review.get("body") or "").strip()
            state = review.get("state", "")
            user = review.get("user", "")
            if body:
                lines.append(f"  Review ({state}) by {user}: {body}")
            elif state in ("APPROVED", "CHANGES_REQUESTED"):
                lines.append(f"  Review ({state}) by {user}: [no comment]")

        for comment in pr.get("review_comments", []):
            body = (comment.get("body") or "").strip()
            path = comment.get("path", "")
            user = comment.get("user", "")
            if body:
                lines.append(f"  Inline on {path} by {user}: {body}")

        # Only include PRs that have actual review content
        if len(lines) > 1:
            sections.append("\n".join(lines))

    return "\n\n".join(sections)


def analyze_reviews_with_cli(
    review_text: str,
    project_path: str,
) -> str:
    """Use Claude CLI (lightweight model) to extract lessons from review text.

    Args:
        review_text: Formatted review text from format_reviews_for_analysis().
        project_path: Path to the git repo (used as cwd for CLI).

    Returns:
        Markdown bullet list of lessons, or empty string on failure.
    """
    from app.cli_provider import build_full_command
    from app.config import get_model_config
    from app.prompts import load_prompt

    prompt = load_prompt("review-learning", REVIEW_DATA=review_text)
    models = get_model_config()

    cmd = build_full_command(
        prompt=prompt,
        allowed_tools=[],
        model=models.get("lightweight", "haiku"),
        fallback=models.get("fallback", "sonnet"),
        max_turns=1,
    )

    from app.cli_exec import run_cli_with_retry

    try:
        result = run_cli_with_retry(
            cmd,
            capture_output=True, text=True,
            timeout=60, cwd=project_path,
        )
        if result.returncode != 0:
            print(
                f"[pr_review_learning] CLI analysis failed: {result.stderr[:200]}",
                file=sys.stderr,
            )
            return ""
        return result.stdout.strip()
    except Exception as e:
        print(f"[pr_review_learning] CLI analysis error: {e}", file=sys.stderr)
        return ""


def _compute_review_hash(prs: List[dict]) -> str:
    """Compute a stable hash of review data to detect changes.

    Uses PR numbers + review/comment bodies to produce a fingerprint.
    If the hash hasn't changed since last run, we skip re-analysis.
    """
    parts = []
    for pr in sorted(prs, key=lambda p: p.get("number", 0)):
        parts.append(str(pr.get("number", "")))
        for review in pr.get("reviews", []):
            parts.append(review.get("body") or "")
        for comment in pr.get("review_comments", []):
            parts.append(comment.get("body") or "")
    content = "|".join(parts)
    return hashlib.sha256(content.encode()).hexdigest()


def _get_cache_path(instance_dir: str) -> Path:
    """Get the path to the review learning cache file."""
    return Path(instance_dir) / ".koan-review-learning-hash"


def _is_cache_fresh(instance_dir: str, current_hash: str) -> bool:
    """Check if the cached hash matches (no new reviews to process)."""
    cache_path = _get_cache_path(instance_dir)
    if not cache_path.exists():
        return False
    try:
        return cache_path.read_text().strip() == current_hash
    except OSError:
        return False


def _write_cache(instance_dir: str, review_hash: str) -> None:
    """Write the review hash to the cache file."""
    try:
        cache_path = _get_cache_path(instance_dir)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        from app.utils import atomic_write
        atomic_write(cache_path, review_hash + "\n")
    except OSError as e:
        print(f"[pr_review_learning] Cache write failed: {e}", file=sys.stderr)


def _append_lessons_to_learnings(
    instance_dir: str,
    project_name: str,
    lessons_text: str,
) -> int:
    """Append new lessons to the project's learnings.md, skipping duplicates.

    Args:
        instance_dir: Path to the instance directory.
        project_name: Project name for scoping.
        lessons_text: Markdown bullet list from Claude analysis.

    Returns:
        Number of new lines appended.
    """
    from app.utils import atomic_write

    learnings_path = (
        Path(instance_dir) / "memory" / "projects" / project_name / "learnings.md"
    )

    # Read existing content
    existing_lines = set()
    existing_content = ""
    if learnings_path.exists():
        try:
            existing_content = learnings_path.read_text(encoding="utf-8")
            existing_lines = {
                line.strip()
                for line in existing_content.splitlines()
                if line.strip() and not line.strip().startswith("#")
            }
        except (OSError, UnicodeDecodeError) as e:
            print(f"[pr_review_learning] Error reading learnings: {e}", file=sys.stderr)

    # Filter out duplicate lessons
    new_lines = []
    for line in lessons_text.splitlines():
        stripped = line.strip()
        if stripped and stripped not in existing_lines:
            new_lines.append(line)

    if not new_lines:
        return 0

    # Ensure directory exists
    learnings_path.parent.mkdir(parents=True, exist_ok=True)

    # Build new content
    date_str = datetime.now().strftime("%Y-%m-%d")
    section = f"\n## PR review learnings ({date_str})\n\n" + "\n".join(new_lines) + "\n"

    if existing_content:
        new_content = existing_content.rstrip("\n") + "\n" + section
    else:
        new_content = f"# Learnings — {project_name}\n" + section

    atomic_write(learnings_path, new_content)
    return len(new_lines)


def learn_from_reviews(
    instance_dir: str,
    project_name: str,
    project_path: str,
    days: int = 30,
    limit: int = 20,
) -> dict:
    """Main entry point: fetch reviews, analyze with Claude, persist to learnings.md.

    This is the function called by the agent loop (e.g., from mission_runner
    or iteration_manager) after a session completes.

    Args:
        instance_dir: Path to the instance directory.
        project_name: Current project name.
        project_path: Path to the git repo.
        days: Look-back window.
        limit: Max PRs to analyze.

    Returns:
        Dict with keys: fetched (int), analyzed (bool), lessons_added (int),
        skipped_reason (str or None).
    """
    result = {"fetched": 0, "analyzed": False, "lessons_added": 0, "skipped_reason": None}

    prs = fetch_pr_reviews(project_path, days=days, limit=limit)
    result["fetched"] = len(prs)
    if not prs:
        result["skipped_reason"] = "no_reviews"
        return result

    # Check cache — skip if reviews haven't changed
    review_hash = _compute_review_hash(prs)
    if _is_cache_fresh(instance_dir, review_hash):
        result["skipped_reason"] = "cache_fresh"
        return result

    # Format reviews for analysis
    review_text = format_reviews_for_analysis(prs)
    if not review_text:
        result["skipped_reason"] = "no_review_content"
        return result

    # Analyze with Claude CLI
    lessons_text = analyze_reviews_with_cli(review_text, project_path)
    result["analyzed"] = True
    if not lessons_text:
        result["skipped_reason"] = "empty_analysis"
        return result

    # Persist to learnings.md
    added = _append_lessons_to_learnings(instance_dir, project_name, lessons_text)
    result["lessons_added"] = added

    # Update cache
    _write_cache(instance_dir, review_hash)
    return result


def _parse_iso(dt_str: str) -> Optional[datetime]:
    """Parse ISO datetime string."""
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
