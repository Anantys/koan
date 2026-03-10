"""Kōan — PR review learning for autonomous alignment.

Extracts actionable patterns from human PR reviews (comments, approvals,
rejections, closures) and surfaces them as lessons for the agent.

The PR feedback system (pr_feedback.py) tracks *merge velocity* — how fast
PRs get merged by category. This module goes deeper: it reads the actual
review comments and actions to learn *what* the human values, critiques,
or rejects.

Signals captured:
- Review comments (inline and top-level)
- Review state (APPROVED, CHANGES_REQUESTED, COMMENTED)
- Closed-without-merge PRs (rejection signal)
- Recurrent themes across multiple reviews

Integration points:
- Read: prompt_builder.py injects lessons into the agent prompt
- Data: GitHub API via gh CLI
"""

import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# Review comment categories — used to bucket feedback patterns
_FEEDBACK_CATEGORIES = {
    "scope": re.compile(
        r"too\s+(?:many|much|big|large)|scope|split|smaller\s+pr|separate\s+pr|one\s+concern",
        re.IGNORECASE,
    ),
    "testing": re.compile(
        r"\btest[s]?\b|coverage|spec[s]?\b|assert|untested|missing\s+test",
        re.IGNORECASE,
    ),
    "style": re.compile(
        r"naming|style|convention|format|indent|readab|lint|typo",
        re.IGNORECASE,
    ),
    "approach": re.compile(
        r"approach|design|architect|pattern|instead|alternative|simpler|overkill|over.?engineer",
        re.IGNORECASE,
    ),
    "dont_touch": re.compile(
        r"don'?t\s+touch|leave\s+(?:it|this)|not\s+(?:needed|necessary|now)|revert|undo",
        re.IGNORECASE,
    ),
    "praise": re.compile(
        r"\bgood\b|nice|great|clean|well\s+done|excellent|solid|perfect|love|👍|🎉|lgtm",
        re.IGNORECASE,
    ),
}


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
    except Exception:
        prefix = "koan/"

    # Fetch merged + closed PRs (both contain review signals)
    prs = []
    for state in ("merged", "closed"):
        try:
            raw = run_gh(
                "pr", "list",
                "--state", state,
                "--limit", str(limit),
                "--json", "number,title,createdAt,mergedAt,closedAt,headRefName,state",
                cwd=project_path,
                timeout=15,
            )
            prs.extend(json.loads(raw))
        except Exception as e:
            print(f"[pr_review_learning] Failed to fetch {state} PRs: {e}",
                  file=sys.stderr)

    # Filter to koan/* branches and recent PRs
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    filtered = []
    seen_numbers = set()
    for pr in prs:
        num = pr.get("number")
        if num in seen_numbers:
            continue
        seen_numbers.add(num)

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


def categorize_feedback(text: str) -> List[str]:
    """Categorize a review comment into feedback types.

    Args:
        text: Review comment body.

    Returns:
        List of matching category names (can match multiple).
    """
    if not text:
        return []

    categories = []
    for category, pattern in _FEEDBACK_CATEGORIES.items():
        if pattern.search(text):
            categories.append(category)

    return categories


def extract_lessons(prs: List[dict]) -> dict:
    """Extract structured lessons from enriched PR data.

    Analyzes review patterns across multiple PRs to identify:
    - Recurring feedback themes
    - What the human approves quickly (positive patterns)
    - What gets rejected or criticized (negative patterns)
    - Areas the human doesn't want touched

    Args:
        prs: List of enriched PR dicts from fetch_pr_reviews().

    Returns:
        Dict with keys:
            feedback_counts: Counter of feedback categories.
            rejected_prs: List of {number, title, reason} for closed-without-merge.
            positive_patterns: List of strings (what gets approved).
            negative_patterns: List of strings (what gets criticized).
            dont_touch_areas: List of strings (areas to avoid).
            review_quotes: List of notable review comments (for prompt injection).
    """
    feedback_counts = Counter()
    rejected_prs = []
    positive_patterns = []
    negative_patterns = []
    dont_touch_areas = []
    review_quotes = []

    for pr in prs:
        all_feedback_text = []

        # Analyze reviews (top-level)
        for review in pr.get("reviews", []):
            body = review.get("body", "") or ""
            state = review.get("state", "")

            if body.strip():
                all_feedback_text.append(body)
                categories = categorize_feedback(body)
                for cat in categories:
                    feedback_counts[cat] += 1

                # Track notable quotes (non-trivial, non-bot)
                if len(body.strip()) > 10 and state != "PENDING":
                    review_quotes.append({
                        "pr": pr.get("number"),
                        "title": pr.get("title", ""),
                        "text": body.strip()[:200],
                        "state": state,
                    })

            # Track approval/rejection patterns
            if state == "CHANGES_REQUESTED":
                categories = categorize_feedback(body)
                for cat in categories:
                    if cat != "praise":
                        negative_patterns.append(
                            f"PR #{pr['number']} ({pr.get('title', '')}): {cat}"
                        )
            elif state == "APPROVED" and body.strip():
                categories = categorize_feedback(body)
                if "praise" in categories:
                    positive_patterns.append(
                        f"PR #{pr['number']} ({pr.get('title', '')}): approved with praise"
                    )

        # Analyze inline comments
        for comment in pr.get("review_comments", []):
            body = comment.get("body", "") or ""
            if body.strip():
                all_feedback_text.append(body)
                categories = categorize_feedback(body)
                for cat in categories:
                    feedback_counts[cat] += 1

                if "dont_touch" in categories:
                    path = comment.get("path", "")
                    dont_touch_areas.append(
                        f"{path}: {body.strip()[:100]}"
                    )

        # Track rejected PRs (closed without merge)
        if not pr.get("was_merged"):
            reason = _infer_rejection_reason(all_feedback_text)
            rejected_prs.append({
                "number": pr.get("number"),
                "title": pr.get("title", ""),
                "reason": reason,
            })

    return {
        "feedback_counts": dict(feedback_counts),
        "rejected_prs": rejected_prs,
        "positive_patterns": positive_patterns[:10],  # Cap
        "negative_patterns": negative_patterns[:10],
        "dont_touch_areas": dont_touch_areas[:5],
        "review_quotes": review_quotes[:10],
    }


def _infer_rejection_reason(feedback_texts: List[str]) -> str:
    """Infer why a PR was rejected from its review comments."""
    if not feedback_texts:
        return "no review comments"

    combined = " ".join(feedback_texts)
    categories = categorize_feedback(combined)

    if "scope" in categories:
        return "scope too large"
    if "approach" in categories:
        return "approach disagreement"
    if "dont_touch" in categories:
        return "area should not be touched"
    if "style" in categories:
        return "style/convention issues"
    if categories:
        return f"feedback on: {', '.join(categories)}"
    return "unclear (review had comments)"


def format_lessons_for_prompt(lessons: dict) -> str:
    """Format extracted lessons as markdown for prompt injection.

    Produces a concise, actionable summary that helps the agent
    avoid past mistakes and repeat successful patterns.

    Args:
        lessons: Dict from extract_lessons().

    Returns:
        Formatted markdown string, or empty string if no lessons.
    """
    lines = []

    # Rejected PRs — strongest signal
    if lessons.get("rejected_prs"):
        lines.append("### Rejected PRs (closed without merge)")
        lines.append("")
        for pr in lessons["rejected_prs"]:
            lines.append(f"- PR #{pr['number']} ({pr['title']}): {pr['reason']}")
        lines.append("")
        lines.append(
            "These PRs were closed without merging. Avoid repeating the same "
            "patterns. Consider why the work was rejected before choosing "
            "similar topics."
        )
        lines.append("")

    # Don't-touch areas
    if lessons.get("dont_touch_areas"):
        lines.append("### Areas to avoid (reviewer feedback)")
        lines.append("")
        for area in lessons["dont_touch_areas"]:
            lines.append(f"- {area}")
        lines.append("")

    # Recurring feedback themes
    counts = lessons.get("feedback_counts", {})
    # Only show categories with 2+ occurrences (patterns, not noise)
    recurring = {k: v for k, v in counts.items() if v >= 2 and k != "praise"}
    if recurring:
        lines.append("### Recurring review feedback")
        lines.append("")
        for cat, count in sorted(recurring.items(), key=lambda x: -x[1]):
            label = _category_label(cat)
            lines.append(f"- **{label}** ({count} occurrences)")
        lines.append("")
        lines.append(
            "Address these patterns proactively in your next PR — "
            "the reviewer has flagged them multiple times."
        )
        lines.append("")

    # Positive patterns (what works well)
    if lessons.get("positive_patterns"):
        lines.append("### What the reviewer values")
        lines.append("")
        for pattern in lessons["positive_patterns"][:5]:
            lines.append(f"- {pattern}")
        lines.append("")

    # Notable quotes (direct voice of the reviewer)
    notable = [q for q in lessons.get("review_quotes", [])
               if q.get("state") in ("CHANGES_REQUESTED", "APPROVED")
               and len(q.get("text", "")) > 20]
    if notable:
        lines.append("### Notable reviewer comments")
        lines.append("")
        for q in notable[:3]:
            lines.append(f"- PR #{q['pr']}: \"{q['text']}\"")
        lines.append("")

    if not lines:
        return ""

    return "\n".join(lines)


def _category_label(category: str) -> str:
    """Human-readable label for a feedback category."""
    labels = {
        "scope": "PR scope too large",
        "testing": "Missing or insufficient tests",
        "style": "Style/convention issues",
        "approach": "Approach/design disagreement",
        "dont_touch": "Area should not be touched",
        "praise": "Positive feedback",
    }
    return labels.get(category, category)


def get_review_lessons(
    project_path: str,
    days: int = 30,
    limit: int = 20,
) -> str:
    """Main entry point: fetch reviews, extract lessons, format for prompt.

    This is the function called by prompt_builder.py.

    Args:
        project_path: Path to the git repo.
        days: Look-back window.
        limit: Max PRs to analyze.

    Returns:
        Formatted markdown string for prompt injection, or empty string.
    """
    prs = fetch_pr_reviews(project_path, days=days, limit=limit)
    if not prs:
        return ""

    lessons = extract_lessons(prs)
    return format_lessons_for_prompt(lessons)


def _parse_iso(dt_str: str) -> Optional[datetime]:
    """Parse ISO datetime string."""
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
