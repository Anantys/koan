#!/usr/bin/env python3
"""
Kōan Deep Research — Intelligent topic selection for DEEP mode

Instead of defaulting to "add tests" or generic refactoring,
this module analyzes project state and suggests priority topics.

Inputs:
- priorities.md: Human-defined focus areas and constraints
- GitHub issues: Open issues for actionable work
- Recent journal: What was recently done (avoid duplicates)
- learnings.md: Known patterns and debt

Output:
- A prioritized list of suggested topics for DEEP mode work
- Reasoning for why each topic is relevant now

Usage:
    deep_research.py <instance_dir> <project_name> <project_path>

Returns JSON with suggested topics and reasoning.
"""

import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path


_STOP_WORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been",
    "for", "and", "or", "but", "in", "on", "at", "to", "of",
    "with", "from", "by", "add", "feat", "fix", "implement",
    "update", "refactor", "test", "github",
})

_BRANCH_ISSUE_RE = re.compile(r"(?:implement|fix|issue)[/-](\d+)")


def _extract_issue_numbers(text: str) -> set[int]:
    """Extract GitHub issue/PR numbers (#NNN) from a string.

    Assumes PR/issue-style text (titles, branch names), not arbitrary markdown.
    """
    return {int(m) for m in re.findall(r"#(\d+)", text)}


def _extract_branch_issue_numbers(branch: str) -> set[int]:
    """Extract issue numbers from branch naming patterns like 'implement-1042'."""
    return {int(m) for m in _BRANCH_ISSUE_RE.findall(branch)}


def _normalize_tokens(text: str) -> set[str]:
    """Extract meaningful lowercase tokens from text for fuzzy matching.

    Strips common noise words to improve overlap detection between
    topic descriptions and PR titles.
    """
    tokens = set(re.findall(r"[a-z]{3,}", text.lower()))
    return tokens - _STOP_WORDS


class DeepResearch:
    """Analyzes project state to suggest meaningful DEEP mode work."""

    def __init__(self, instance_dir: Path, project_name: str, project_path: Path):
        self.instance = instance_dir
        self.project_name = project_name
        self.project_path = project_path
        self.memory_dir = instance_dir / "memory" / "projects" / project_name
        self._pending_prs: list[dict] | None = None

    def get_priorities(self) -> dict:
        """Parse priorities.md into structured data."""
        priorities_file = self.memory_dir / "priorities.md"
        if not priorities_file.exists():
            return {
                "current_focus": [],
                "strategic_goals": [],
                "technical_debt": [],
                "do_not_touch": [],
                "notes": "",
            }

        content = priorities_file.read_text()

        def extract_section(header: str) -> list[str]:
            """Extract list items from a markdown section."""
            # Match from header to next ## header (or end of file)
            pattern = rf"## {header}\s*(?:<!--.*?-->)?\s*(.*?)(?=\n## |\Z)"
            match = re.search(pattern, content, re.IGNORECASE | re.DOTALL)
            if not match:
                return []
            items = []
            for line in match.group(1).split("\n"):
                line = line.strip()
                if line.startswith("- ") and line != "- ":
                    item = line[2:].strip()
                    # Skip placeholder items
                    if not item.startswith("(") or not item.endswith(")"):
                        items.append(item)
            return items

        def extract_notes() -> str:
            """Extract notes section content."""
            pattern = r"## Notes\s*(?:<!--.*?-->)?\s*(.+?)(?=\n##|$)"
            match = re.search(pattern, content, re.IGNORECASE | re.DOTALL)
            if not match:
                return ""
            text = match.group(1).strip()
            # Skip placeholder
            if text.startswith("(") and text.endswith(")"):
                return ""
            return text

        return {
            "current_focus": extract_section("Current Focus"),
            "strategic_goals": extract_section("Strategic Goals"),
            "technical_debt": extract_section("Technical Debt"),
            "do_not_touch": extract_section("Do Not Touch"),
            "notes": extract_notes(),
        }

    def get_open_issues(self, limit: int = 10) -> list[dict]:
        """Fetch open GitHub issues for the project."""
        try:
            from app.github import run_gh
            output = run_gh(
                "issue", "list",
                "--state", "open",
                "--limit", str(limit),
                "--json", "number,title,labels,createdAt",
                cwd=self.project_path,
            )
            return json.loads(output)
        except Exception as e:
            print(f"[deep_research] Issue fetch failed: {e}", file=sys.stderr)
            return []

    def get_pending_prs(self) -> list[dict]:
        """Fetch open PRs that might need attention.

        Results are cached for the lifetime of this DeepResearch instance
        to avoid redundant gh API calls within a single analysis run.
        """
        if self._pending_prs is not None:
            return self._pending_prs
        try:
            from app.github import run_gh
            output = run_gh(
                "pr", "list",
                "--state", "open",
                "--json", "number,title,createdAt,headRefName",
                cwd=self.project_path,
            )
            self._pending_prs = json.loads(output)
        except Exception as e:
            print(f"[deep_research] PR fetch failed: {e}", file=sys.stderr)
            self._pending_prs = []
        return self._pending_prs

    def _build_pr_coverage(self) -> dict:
        """Build a coverage map from open PRs.

        Returns:
            Dict with keys:
            - issue_numbers: set of int — issue numbers referenced in PR titles/branches
            - pr_tokens: dict mapping PR number to normalized token set
            - prs: list of PR dicts (for display)
        """
        prs = self.get_pending_prs()
        covered_issues: set[int] = set()
        pr_tokens: dict[int, set[str]] = {}

        for pr in prs:
            title = pr.get("title", "")
            branch = pr.get("headRefName", "")
            number = pr.get("number", 0)

            # Extract issue numbers from title and branch
            covered_issues |= _extract_issue_numbers(title)
            covered_issues |= _extract_issue_numbers(branch)

            # Also extract issue numbers from branch patterns like "implement-1042"
            covered_issues |= _extract_branch_issue_numbers(branch)

            # Build token set for fuzzy matching
            pr_tokens[number] = _normalize_tokens(title) | _normalize_tokens(branch)

        return {
            "issue_numbers": covered_issues,
            "pr_tokens": pr_tokens,
            "prs": prs,
        }

    def _topic_has_open_pr(self, topic: str, coverage: dict) -> int | None:
        """Check if a topic is already covered by an open PR.

        Returns the PR number if covered, None otherwise.

        Matching strategy:
        1. Exact issue number match (strongest signal)
        2. Significant token overlap (>= 50% of topic tokens match a PR)
        """
        # 1. Issue number match
        topic_issues = _extract_issue_numbers(topic)
        overlap = topic_issues & coverage["issue_numbers"]
        if overlap:
            # Find which PR covers this issue
            for pr in coverage["prs"]:
                pr_title = pr.get("title", "")
                pr_branch = pr.get("headRefName", "")
                pr_issues = _extract_issue_numbers(pr_title) | _extract_issue_numbers(pr_branch)
                pr_issues |= _extract_branch_issue_numbers(pr_branch)
                if pr_issues & topic_issues:
                    return pr.get("number")

        # 2. Token overlap (fuzzy match)
        topic_tokens = _normalize_tokens(topic)
        if len(topic_tokens) < 2:
            return None  # Too few tokens for reliable matching

        for pr_num, pr_toks in coverage["pr_tokens"].items():
            if not pr_toks:
                continue
            common = topic_tokens & pr_toks
            # Require >= 50% of topic tokens to match
            if len(common) >= max(2, len(topic_tokens) * 0.5):
                return pr_num

        return None

    def get_recent_journal_topics(self, days: int = 7) -> list[str]:
        """Extract topics from recent journal entries to avoid repetition."""
        topics = []
        journal_dir = self.instance / "journal"

        for i in range(days):
            date = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            journal_file = journal_dir / date / f"{self.project_name}.md"
            if journal_file.exists():
                content = journal_file.read_text()
                # Extract session headers (## Session N, ## Run N, etc.)
                for match in re.finditer(r"^##\s*(.+?)$", content, re.MULTILINE):
                    topics.append(match.group(1).strip())

        return topics

    def get_known_learnings(self) -> list[str]:
        """Extract key learnings that might inform priorities."""
        learnings_file = self.memory_dir / "learnings.md"
        if not learnings_file.exists():
            return []

        content = learnings_file.read_text()
        # Extract section headers (## Something)
        return re.findall(r"^## (.+?)$", content, re.MULTILINE)

    def get_pr_feedback(self) -> dict:
        """Get PR merge feedback for this project.

        Returns:
            Dict with keys:
            - alignment_summary: str (formatted for prompt)
            - category_boosts: dict (category → priority adjustment)
        """
        try:
            from app.pr_feedback import get_alignment_summary, get_category_boost
            summary = get_alignment_summary(str(self.project_path))
            boosts = get_category_boost(str(self.project_path))
            return {
                "alignment_summary": summary,
                "category_boosts": boosts,
            }
        except Exception as e:
            print(f"[deep_research] PR feedback failed: {e}", file=sys.stderr)
            return {"alignment_summary": "", "category_boosts": {}}

    def _match_topic_to_category(self, topic: str) -> str:
        """Best-effort match a topic string to a PR work category.

        Uses the same categorization logic as pr_feedback.categorize_pr()
        to enable feedback-based priority adjustment.
        """
        try:
            from app.pr_feedback import categorize_pr
            return categorize_pr(topic)
        except Exception as e:
            print(f"[deep_research] Topic categorization failed: {e}", file=sys.stderr)
            return "other"

    def suggest_topics(self) -> list[dict]:
        """
        Analyze all sources and suggest prioritized topics.

        Returns a list of suggested topics with reasoning.
        Each item has: topic, source, reasoning, priority (1-3)
        """
        suggestions = []
        priorities = self.get_priorities()
        issues = self.get_open_issues()
        recent_topics = self.get_recent_journal_topics()

        # Priority 1: Current focus items from priorities.md
        for item in priorities.get("current_focus", []):
            suggestions.append({
                "topic": item,
                "source": "priorities.md (Current Focus)",
                "reasoning": "Explicitly marked as current priority by human",
                "priority": 1,
            })

        # Priority 2: Open GitHub issues (if any)
        for issue in issues[:5]:  # Top 5 issues
            title = issue.get("title", "")
            labels = [l.get("name", "") for l in issue.get("labels", [])]

            # Skip if recently worked on
            if any(title.lower() in t.lower() for t in recent_topics):
                continue

            priority = 2
            if "bug" in labels or "critical" in labels:
                priority = 1
            elif "enhancement" in labels or "feature" in labels:
                priority = 2
            else:
                priority = 3

            suggestions.append({
                "topic": f"GitHub #{issue['number']}: {title}",
                "source": "GitHub Issues",
                "reasoning": f"Open issue with labels: {', '.join(labels) or 'none'}",
                "priority": priority,
            })

        # Priority 2-3: Technical debt items
        for item in priorities.get("technical_debt", []):
            # Skip if recently worked on
            if any(item.lower() in t.lower() for t in recent_topics):
                continue
            suggestions.append({
                "topic": item,
                "source": "priorities.md (Technical Debt)",
                "reasoning": "Known tech debt item, good for DEEP mode",
                "priority": 2,
            })

        # Priority 3: Strategic goals (bigger picture)
        for item in priorities.get("strategic_goals", []):
            suggestions.append({
                "topic": item,
                "source": "priorities.md (Strategic Goals)",
                "reasoning": "Contributes to larger project direction",
                "priority": 3,
            })

        # Filter out topics already covered by open PRs
        coverage = self._build_pr_coverage()
        filtered = []
        for suggestion in suggestions:
            pr_num = self._topic_has_open_pr(suggestion["topic"], coverage)
            if pr_num is not None:
                # Skip entirely — there's already a PR for this
                print(
                    f"[deep_research] Skipping '{suggestion['topic'][:60]}' "
                    f"— covered by PR #{pr_num}",
                    file=sys.stderr,
                )
                continue
            filtered.append(suggestion)
        suggestions = filtered

        # Apply PR merge feedback to adjust priorities
        feedback = self.get_pr_feedback()
        boosts = feedback.get("category_boosts", {})
        if boosts:
            for suggestion in suggestions:
                category = self._match_topic_to_category(suggestion["topic"])
                adjustment = boosts.get(category, 0)
                if adjustment != 0:
                    old_prio = suggestion["priority"]
                    suggestion["priority"] = max(1, min(3, old_prio + adjustment))
                    if adjustment < 0:
                        suggestion["reasoning"] += " (boosted: this type of work gets merged quickly)"
                    else:
                        suggestion["reasoning"] += " (deprioritized: this type of work tends to stay open)"

        # Sort by priority
        suggestions.sort(key=lambda x: x["priority"])

        return suggestions

    def get_do_not_touch(self) -> list[str]:
        """Return areas to avoid."""
        priorities = self.get_priorities()
        return priorities.get("do_not_touch", [])

    def get_staleness_warning(self) -> str:
        """Check session outcome history for staleness patterns.

        Returns a warning string if recent sessions were non-productive,
        empty string otherwise.
        """
        try:
            from app.session_tracker import get_staleness_warning
            return get_staleness_warning(str(self.instance), self.project_name)
        except Exception as e:
            print(f"[deep_research] Staleness check failed: {e}", file=sys.stderr)
            return ""

    def format_for_agent(self) -> str:
        """
        Format suggestions as markdown for injection into agent prompt.
        """
        suggestions = self.suggest_topics()
        do_not_touch = self.get_do_not_touch()
        priorities = self.get_priorities()
        staleness = self.get_staleness_warning()

        if not suggestions and not do_not_touch and not staleness:
            return ""

        lines = ["## Deep Research Suggestions", ""]

        # Staleness warning (highest priority — shown first)
        if staleness:
            lines.append(staleness)
            lines.append("")

        if priorities.get("notes"):
            lines.append(f"**Context**: {priorities['notes']}")
            lines.append("")

        if suggestions:
            lines.append("### Suggested Topics (prioritized)")
            lines.append("")
            for i, s in enumerate(suggestions[:5], 1):  # Top 5
                prio_marker = "🔴" if s["priority"] == 1 else "🟡" if s["priority"] == 2 else "🟢"
                lines.append(f"{i}. {prio_marker} **{s['topic']}**")
                lines.append(f"   - Source: {s['source']}")
                lines.append(f"   - Why now: {s['reasoning']}")
                lines.append("")
        else:
            lines.append("No specific suggestions — use your judgment on what would be most valuable.")
            lines.append("")

        # PR merge feedback (what work gets valued)
        feedback = self.get_pr_feedback()
        alignment = feedback.get("alignment_summary", "")
        if alignment:
            lines.append("### PR Merge Feedback (what the human merges quickly)")
            lines.append("")
            lines.append(alignment)
            lines.append("")

        # In-flight work (open PRs) — helps avoid duplicate work
        pending_prs = self.get_pending_prs()
        if pending_prs:
            lines.append("### In-Flight Work (open PRs)")
            lines.append("")
            lines.append("These PRs are already open — avoid duplicating this work:")
            for pr in pending_prs[:8]:  # Cap at 8 to keep prompt lean
                title = pr.get("title", "")
                number = pr.get("number", "")
                lines.append(f"- PR #{number}: {title}")
            if len(pending_prs) > 8:
                lines.append(f"- ... and {len(pending_prs) - 8} more")
            lines.append("")

        if do_not_touch:
            lines.append("### Avoid These Areas")
            lines.append("")
            for item in do_not_touch:
                lines.append(f"- {item}")
            lines.append("")

        lines.append("---")
        lines.append("")
        lines.append("Choose ONE topic and go deep. Document your reasoning in the journal.")
        lines.append("If none of these fit, propose your own topic (and update priorities.md with what you find).")

        return "\n".join(lines)

    def to_json(self) -> str:
        """Return all analysis as JSON."""
        feedback = self.get_pr_feedback()
        pending_prs = self.get_pending_prs()
        return json.dumps({
            "priorities": self.get_priorities(),
            "suggestions": self.suggest_topics(),
            "do_not_touch": self.get_do_not_touch(),
            "open_issues": self.get_open_issues(),
            "pending_prs": [
                {"number": pr.get("number"), "title": pr.get("title")}
                for pr in pending_prs
            ],
            "recent_topics": self.get_recent_journal_topics(),
            "pr_feedback": {
                "alignment_summary": feedback.get("alignment_summary", ""),
                "category_boosts": feedback.get("category_boosts", {}),
            },
        }, indent=2)


def main():
    """CLI entry point."""
    if len(sys.argv) < 4:
        print("Usage: deep_research.py <instance_dir> <project_name> <project_path> [--json|--markdown]")
        sys.exit(1)

    instance_dir = Path(sys.argv[1])
    project_name = sys.argv[2]
    project_path = Path(sys.argv[3])
    output_format = sys.argv[4] if len(sys.argv) > 4 else "--markdown"

    research = DeepResearch(instance_dir, project_name, project_path)

    if output_format == "--json":
        print(research.to_json())
    else:
        print(research.format_for_agent())


if __name__ == "__main__":
    main()
