"""Tests for deep_research.py — DEEP mode topic suggestion system."""

import json
import subprocess
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from app.deep_research import DeepResearch, _extract_issue_numbers, _normalize_tokens

pytestmark = pytest.mark.slow


@pytest.fixture(autouse=True)
def _no_pending_prs(monkeypatch):
    """Default: no open PRs for PR-aware topic filtering.

    Tests that need open PRs should set research._pending_prs directly.
    This prevents existing tests from being affected by the PR coverage
    filter when they patch subprocess.run for other gh CLI calls.
    """
    original_init = DeepResearch.__init__

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self._pending_prs = []

    monkeypatch.setattr(DeepResearch, "__init__", patched_init)


@pytest.fixture
def research_env(tmp_path):
    """Create a minimal environment for DeepResearch testing."""
    instance = tmp_path / "instance"
    project_path = tmp_path / "project"
    project_name = "testproj"

    # Create directory structure
    (instance / "memory" / "projects" / project_name).mkdir(parents=True)
    (instance / "journal").mkdir(parents=True)
    project_path.mkdir()

    return {
        "instance": instance,
        "project_path": project_path,
        "project_name": project_name,
    }


class TestGetPriorities:
    """Tests for priorities.md parsing."""

    def test_missing_priorities_file(self, research_env):
        """Returns empty sections when priorities.md doesn't exist."""
        research = DeepResearch(
            research_env["instance"],
            research_env["project_name"],
            research_env["project_path"],
        )

        result = research.get_priorities()

        assert result["current_focus"] == []
        assert result["strategic_goals"] == []
        assert result["technical_debt"] == []
        assert result["do_not_touch"] == []
        assert result["notes"] == ""

    def test_parse_all_sections(self, research_env):
        """Parses all sections from priorities.md."""
        priorities_content = """# Project Priorities

## Current Focus

- Implement user authentication
- Fix critical bug in checkout

## Strategic Goals

- Launch mobile app
- Improve test coverage

## Technical Debt

- Refactor legacy payment module
- Remove deprecated API endpoints

## Do Not Touch

- Legacy billing code (being rewritten)
- Mobile app (separate team)

## Notes

We're preparing for launch next month.
"""
        priorities_file = (
            research_env["instance"]
            / "memory"
            / "projects"
            / research_env["project_name"]
            / "priorities.md"
        )
        priorities_file.write_text(priorities_content)

        research = DeepResearch(
            research_env["instance"],
            research_env["project_name"],
            research_env["project_path"],
        )

        result = research.get_priorities()

        assert "Implement user authentication" in result["current_focus"]
        assert "Fix critical bug in checkout" in result["current_focus"]
        assert "Launch mobile app" in result["strategic_goals"]
        assert "Refactor legacy payment module" in result["technical_debt"]
        assert "Legacy billing code (being rewritten)" in result["do_not_touch"]
        assert "launch next month" in result["notes"]

    def test_skips_placeholder_items(self, research_env):
        """Skips items that are just placeholders like (What to do?)."""
        priorities_content = """# Project Priorities

## Current Focus

- (What's the most important thing?)
- Real task here

## Technical Debt

- (Known issues?)
"""
        priorities_file = (
            research_env["instance"]
            / "memory"
            / "projects"
            / research_env["project_name"]
            / "priorities.md"
        )
        priorities_file.write_text(priorities_content)

        research = DeepResearch(
            research_env["instance"],
            research_env["project_name"],
            research_env["project_path"],
        )

        result = research.get_priorities()

        assert len(result["current_focus"]) == 1
        assert "Real task here" in result["current_focus"]
        assert len(result["technical_debt"]) == 0

    def test_handles_html_comments(self, research_env):
        """Ignores HTML comment blocks in sections."""
        priorities_content = """## Current Focus

<!--
This is a template comment.
It should be ignored.
-->

- Actual priority item
"""
        priorities_file = (
            research_env["instance"]
            / "memory"
            / "projects"
            / research_env["project_name"]
            / "priorities.md"
        )
        priorities_file.write_text(priorities_content)

        research = DeepResearch(
            research_env["instance"],
            research_env["project_name"],
            research_env["project_path"],
        )

        result = research.get_priorities()

        assert len(result["current_focus"]) == 1
        assert "Actual priority item" in result["current_focus"]


class TestGitHubIntegration:
    """Tests for GitHub issue/PR fetching."""

    def test_get_open_issues_success(self, research_env):
        """Parses GitHub issues from gh CLI output."""
        mock_issues = [
            {"number": 42, "title": "Bug in login", "labels": [{"name": "bug"}], "createdAt": "2024-01-01"},
            {"number": 43, "title": "Add feature X", "labels": [{"name": "enhancement"}], "createdAt": "2024-01-02"},
        ]

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps(mock_issues),
            )

            research = DeepResearch(
                research_env["instance"],
                research_env["project_name"],
                research_env["project_path"],
            )

            result = research.get_open_issues()

            assert len(result) == 2
            assert result[0]["number"] == 42
            assert result[1]["title"] == "Add feature X"

    def test_get_open_issues_gh_not_available(self, research_env):
        """Returns empty list when gh CLI is not available."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError()

            research = DeepResearch(
                research_env["instance"],
                research_env["project_name"],
                research_env["project_path"],
            )

            result = research.get_open_issues()

            assert result == []

    def test_get_open_issues_timeout(self, research_env):
        """Returns empty list on timeout."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="gh", timeout=30)

            research = DeepResearch(
                research_env["instance"],
                research_env["project_name"],
                research_env["project_path"],
            )

            result = research.get_open_issues()

            assert result == []

    def test_get_pending_prs(self, research_env):
        """Parses open PRs from gh CLI output."""
        mock_prs = [
            {"number": 100, "title": "Fix typo", "createdAt": "2024-01-01", "headRefName": "koan/fix-typo"},
        ]

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps(mock_prs),
            )

            research = DeepResearch(
                research_env["instance"],
                research_env["project_name"],
                research_env["project_path"],
            )
            research._pending_prs = None  # Reset autouse cache

            result = research.get_pending_prs()

            assert len(result) == 1
            assert result[0]["headRefName"] == "koan/fix-typo"


class TestJournalAnalysis:
    """Tests for recent journal topic extraction."""

    def test_extracts_session_headers(self, research_env):
        """Extracts ## headers from journal files."""
        today = datetime.now().strftime("%Y-%m-%d")
        journal_dir = research_env["instance"] / "journal" / today
        journal_dir.mkdir(parents=True)
        journal_file = journal_dir / f"{research_env['project_name']}.md"
        journal_file.write_text("""# Journal

## Session 100 — Test coverage expansion

Did some tests.

## Session 101 — Authentication refactor

Refactored auth.
""")

        research = DeepResearch(
            research_env["instance"],
            research_env["project_name"],
            research_env["project_path"],
        )

        result = research.get_recent_journal_topics()

        assert "Session 100 — Test coverage expansion" in result
        assert "Session 101 — Authentication refactor" in result

    def test_no_journal_files(self, research_env):
        """Returns empty list when no journal files exist."""
        research = DeepResearch(
            research_env["instance"],
            research_env["project_name"],
            research_env["project_path"],
        )

        result = research.get_recent_journal_topics()

        assert result == []


class TestTopicSuggestions:
    """Tests for the suggestion algorithm."""

    def test_prioritizes_current_focus(self, research_env):
        """Current focus items get priority 1."""
        priorities_content = """## Current Focus

- Critical bug fix

## Technical Debt

- Refactor module
"""
        priorities_file = (
            research_env["instance"]
            / "memory"
            / "projects"
            / research_env["project_name"]
            / "priorities.md"
        )
        priorities_file.write_text(priorities_content)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)  # gh fails

            research = DeepResearch(
                research_env["instance"],
                research_env["project_name"],
                research_env["project_path"],
            )

            result = research.suggest_topics()

            assert result[0]["priority"] == 1
            assert "Critical bug fix" in result[0]["topic"]

    def test_bug_issues_get_high_priority(self, research_env):
        """GitHub issues labeled 'bug' get priority 1."""
        mock_issues = [
            {"number": 1, "title": "Feature request", "labels": [{"name": "enhancement"}], "createdAt": "2024-01-01"},
            {"number": 2, "title": "Critical bug", "labels": [{"name": "bug"}], "createdAt": "2024-01-01"},
        ]

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps(mock_issues),
            )

            research = DeepResearch(
                research_env["instance"],
                research_env["project_name"],
                research_env["project_path"],
            )

            result = research.suggest_topics()

            # Bug should be first (priority 1)
            bug_suggestion = next(s for s in result if "#2" in s["topic"])
            feature_suggestion = next(s for s in result if "#1" in s["topic"])

            assert bug_suggestion["priority"] == 1
            assert feature_suggestion["priority"] == 2

    def test_skips_recently_worked_topics(self, research_env):
        """Skips issues that match recent journal topics."""
        today = datetime.now().strftime("%Y-%m-%d")
        journal_dir = research_env["instance"] / "journal" / today
        journal_dir.mkdir(parents=True)
        journal_file = journal_dir / f"{research_env['project_name']}.md"
        journal_file.write_text("## Session — Test coverage expansion\n")

        mock_issues = [
            {"number": 1, "title": "Test coverage expansion", "labels": [], "createdAt": "2024-01-01"},
            {"number": 2, "title": "New feature", "labels": [], "createdAt": "2024-01-01"},
        ]

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps(mock_issues),
            )

            research = DeepResearch(
                research_env["instance"],
                research_env["project_name"],
                research_env["project_path"],
            )

            result = research.suggest_topics()

            # Should only have the "New feature" issue
            titles = [s["topic"] for s in result]
            assert not any("Test coverage" in t for t in titles)
            assert any("#2" in t for t in titles)


class TestOutputFormatting:
    """Tests for output formatting."""

    def test_format_for_agent_with_suggestions(self, research_env):
        """Generates markdown with suggestions."""
        priorities_content = """## Current Focus

- Priority one

## Notes

Context for the agent.
"""
        priorities_file = (
            research_env["instance"]
            / "memory"
            / "projects"
            / research_env["project_name"]
            / "priorities.md"
        )
        priorities_file.write_text(priorities_content)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)

            research = DeepResearch(
                research_env["instance"],
                research_env["project_name"],
                research_env["project_path"],
            )

            result = research.format_for_agent()

            assert "## Deep Research Suggestions" in result
            assert "Priority one" in result
            assert "Context for the agent" in result
            assert "Choose ONE topic" in result

    def test_format_for_agent_with_do_not_touch(self, research_env):
        """Includes do-not-touch areas in output."""
        priorities_content = """## Do Not Touch

- Legacy billing module
"""
        priorities_file = (
            research_env["instance"]
            / "memory"
            / "projects"
            / research_env["project_name"]
            / "priorities.md"
        )
        priorities_file.write_text(priorities_content)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)

            research = DeepResearch(
                research_env["instance"],
                research_env["project_name"],
                research_env["project_path"],
            )

            result = research.format_for_agent()

            assert "### Avoid These Areas" in result
            assert "Legacy billing module" in result

    def test_format_for_agent_empty(self, research_env):
        """Returns empty string when no suggestions or constraints."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)

            research = DeepResearch(
                research_env["instance"],
                research_env["project_name"],
                research_env["project_path"],
            )

            result = research.format_for_agent()

            assert result == ""

    def test_to_json_includes_all_data(self, research_env):
        """JSON output includes all analyzed data."""
        priorities_content = """## Current Focus

- Task one
"""
        priorities_file = (
            research_env["instance"]
            / "memory"
            / "projects"
            / research_env["project_name"]
            / "priorities.md"
        )
        priorities_file.write_text(priorities_content)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)

            research = DeepResearch(
                research_env["instance"],
                research_env["project_name"],
                research_env["project_path"],
            )

            result = json.loads(research.to_json())

            assert "priorities" in result
            assert "suggestions" in result
            assert "do_not_touch" in result
            assert "open_issues" in result
            assert "recent_topics" in result


# ---------------------------------------------------------------------------
# get_known_learnings
# ---------------------------------------------------------------------------

class TestGetKnownLearnings:
    """Tests for learnings.md section header extraction."""

    def test_extracts_section_headers(self, research_env):
        """Parses ## headers from learnings.md."""
        learnings_file = (
            research_env["instance"]
            / "memory"
            / "projects"
            / research_env["project_name"]
            / "learnings.md"
        )
        learnings_file.write_text(
            "# Learnings\n\n"
            "## Architecture\n\nSome notes.\n\n"
            "## Test Patterns\n\nMore notes.\n\n"
            "## Common Pitfalls\n\nWatch out.\n"
        )

        research = DeepResearch(
            research_env["instance"],
            research_env["project_name"],
            research_env["project_path"],
        )

        result = research.get_known_learnings()

        assert "Architecture" in result
        assert "Test Patterns" in result
        assert "Common Pitfalls" in result
        assert len(result) == 3

    def test_missing_learnings_file(self, research_env):
        """Returns empty list when learnings.md doesn't exist."""
        research = DeepResearch(
            research_env["instance"],
            research_env["project_name"],
            research_env["project_path"],
        )

        result = research.get_known_learnings()

        assert result == []

    def test_empty_learnings_file(self, research_env):
        """Returns empty list for a file with no ## headers."""
        learnings_file = (
            research_env["instance"]
            / "memory"
            / "projects"
            / research_env["project_name"]
            / "learnings.md"
        )
        learnings_file.write_text("# Learnings\n\nNo sections yet.\n")

        research = DeepResearch(
            research_env["instance"],
            research_env["project_name"],
            research_env["project_path"],
        )

        result = research.get_known_learnings()

        assert result == []


# ---------------------------------------------------------------------------
# get_do_not_touch (direct)
# ---------------------------------------------------------------------------

class TestGetDoNotTouch:
    """Tests for the do_not_touch convenience method."""

    def test_returns_do_not_touch_items(self, research_env):
        """Returns items from Do Not Touch section."""
        priorities_file = (
            research_env["instance"]
            / "memory"
            / "projects"
            / research_env["project_name"]
            / "priorities.md"
        )
        priorities_file.write_text("## Do Not Touch\n\n- Legacy billing\n- Mobile app\n")

        research = DeepResearch(
            research_env["instance"],
            research_env["project_name"],
            research_env["project_path"],
        )

        result = research.get_do_not_touch()

        assert "Legacy billing" in result
        assert "Mobile app" in result

    def test_empty_when_no_section(self, research_env):
        """Returns empty list when no Do Not Touch section exists."""
        research = DeepResearch(
            research_env["instance"],
            research_env["project_name"],
            research_env["project_path"],
        )

        result = research.get_do_not_touch()

        assert result == []


# ---------------------------------------------------------------------------
# get_staleness_warning
# ---------------------------------------------------------------------------

class TestGetStalenessWarning:
    """Tests for staleness warning from session tracker."""

    def test_returns_warning_when_stale(self, research_env):
        """Returns warning string from session_tracker when stale."""
        with patch("app.session_tracker.get_staleness_warning", return_value="⚠️ 5 consecutive empty sessions"):
            research = DeepResearch(
                research_env["instance"],
                research_env["project_name"],
                research_env["project_path"],
            )

            result = research.get_staleness_warning()

            assert "5 consecutive" in result

    def test_returns_empty_when_fresh(self, research_env):
        """Returns empty string when not stale."""
        with patch("app.session_tracker.get_staleness_warning", return_value=""):
            research = DeepResearch(
                research_env["instance"],
                research_env["project_name"],
                research_env["project_path"],
            )

            result = research.get_staleness_warning()

            assert result == ""

    def test_returns_empty_on_import_error(self, research_env):
        """Returns empty string when session_tracker import fails."""
        with patch.dict("sys.modules", {"app.session_tracker": None}):
            research = DeepResearch(
                research_env["instance"],
                research_env["project_name"],
                research_env["project_path"],
            )

            result = research.get_staleness_warning()

            assert result == ""

    def test_returns_empty_on_exception(self, research_env):
        """Returns empty string when staleness check raises."""
        with patch("app.session_tracker.get_staleness_warning", side_effect=RuntimeError("db error")):
            research = DeepResearch(
                research_env["instance"],
                research_env["project_name"],
                research_env["project_path"],
            )

            result = research.get_staleness_warning()

            assert result == ""


# ---------------------------------------------------------------------------
# suggest_topics — additional edge cases
# ---------------------------------------------------------------------------

class TestSuggestTopicsEdgeCases:
    """Additional edge cases for the suggestion algorithm."""

    def test_strategic_goals_get_priority_3(self, research_env):
        """Strategic goals are always priority 3."""
        priorities_file = (
            research_env["instance"]
            / "memory"
            / "projects"
            / research_env["project_name"]
            / "priorities.md"
        )
        priorities_file.write_text("## Strategic Goals\n\n- Long-term refactor\n- Launch v2\n")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)

            research = DeepResearch(
                research_env["instance"],
                research_env["project_name"],
                research_env["project_path"],
            )

            result = research.suggest_topics()

            assert len(result) == 2
            assert all(s["priority"] == 3 for s in result)
            assert all(s["source"] == "priorities.md (Strategic Goals)" for s in result)

    def test_issues_without_labels_get_priority_3(self, research_env):
        """GitHub issues with no recognized labels get priority 3."""
        mock_issues = [
            {"number": 10, "title": "Something random", "labels": [], "createdAt": "2024-01-01"},
        ]

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps(mock_issues),
            )

            research = DeepResearch(
                research_env["instance"],
                research_env["project_name"],
                research_env["project_path"],
            )

            result = research.suggest_topics()

            assert len(result) == 1
            assert result[0]["priority"] == 3

    def test_critical_label_gets_priority_1(self, research_env):
        """Issues labeled 'critical' get priority 1."""
        mock_issues = [
            {"number": 5, "title": "Server crash", "labels": [{"name": "critical"}], "createdAt": "2024-01-01"},
        ]

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps(mock_issues),
            )

            research = DeepResearch(
                research_env["instance"],
                research_env["project_name"],
                research_env["project_path"],
            )

            result = research.suggest_topics()

            assert result[0]["priority"] == 1

    def test_limits_issues_to_top_5(self, research_env):
        """Only first 5 issues are included in suggestions."""
        mock_issues = [
            {"number": i, "title": f"Issue {i}", "labels": [], "createdAt": "2024-01-01"}
            for i in range(10)
        ]

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps(mock_issues),
            )

            research = DeepResearch(
                research_env["instance"],
                research_env["project_name"],
                research_env["project_path"],
            )

            result = research.suggest_topics()

            issue_topics = [s for s in result if s["source"] == "GitHub Issues"]
            assert len(issue_topics) == 5

    def test_technical_debt_dedup_with_journal(self, research_env):
        """Technical debt items matching recent journal topics are skipped."""
        priorities_file = (
            research_env["instance"]
            / "memory"
            / "projects"
            / research_env["project_name"]
            / "priorities.md"
        )
        priorities_file.write_text(
            "## Technical Debt\n\n"
            "- Refactor payment module\n"
            "- Clean up test fixtures\n"
        )

        today = datetime.now().strftime("%Y-%m-%d")
        journal_dir = research_env["instance"] / "journal" / today
        journal_dir.mkdir(parents=True)
        journal_file = journal_dir / f"{research_env['project_name']}.md"
        journal_file.write_text("## Session — Refactor payment module\n")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)

            research = DeepResearch(
                research_env["instance"],
                research_env["project_name"],
                research_env["project_path"],
            )

            result = research.suggest_topics()

            topics = [s["topic"] for s in result]
            assert "Clean up test fixtures" in topics
            assert "Refactor payment module" not in topics

    def test_empty_with_no_sources(self, research_env):
        """Returns empty list when no priorities, no issues, no debt."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)

            research = DeepResearch(
                research_env["instance"],
                research_env["project_name"],
                research_env["project_path"],
            )

            result = research.suggest_topics()

            assert result == []

    def test_sorted_by_priority(self, research_env):
        """Suggestions are sorted by priority (1 first, 3 last)."""
        priorities_file = (
            research_env["instance"]
            / "memory"
            / "projects"
            / research_env["project_name"]
            / "priorities.md"
        )
        priorities_file.write_text(
            "## Strategic Goals\n\n- Big vision\n\n"
            "## Current Focus\n\n- Urgent task\n\n"
            "## Technical Debt\n\n- Cleanup\n"
        )

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)

            research = DeepResearch(
                research_env["instance"],
                research_env["project_name"],
                research_env["project_path"],
            )

            result = research.suggest_topics()

            priorities = [s["priority"] for s in result]
            assert priorities == sorted(priorities)
            assert result[0]["topic"] == "Urgent task"


# ---------------------------------------------------------------------------
# format_for_agent — additional paths
# ---------------------------------------------------------------------------

class TestFormatForAgentEdgeCases:
    """Additional edge cases for agent formatting."""

    def test_includes_staleness_warning(self, research_env):
        """Staleness warning appears at top of output."""
        priorities_file = (
            research_env["instance"]
            / "memory"
            / "projects"
            / research_env["project_name"]
            / "priorities.md"
        )
        priorities_file.write_text("## Current Focus\n\n- Task\n")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)

            with patch("app.session_tracker.get_staleness_warning", return_value="⚠️ Stale project warning"):
                research = DeepResearch(
                    research_env["instance"],
                    research_env["project_name"],
                    research_env["project_path"],
                )

                result = research.format_for_agent()

                assert "⚠️ Stale project warning" in result
                # Staleness warning should appear before suggestions
                stale_pos = result.index("Stale project")
                suggest_pos = result.index("Suggested Topics")
                assert stale_pos < suggest_pos

    def test_emoji_markers_by_priority(self, research_env):
        """Priority 1=🔴, 2=🟡, 3=🟢."""
        priorities_file = (
            research_env["instance"]
            / "memory"
            / "projects"
            / research_env["project_name"]
            / "priorities.md"
        )
        priorities_file.write_text(
            "## Current Focus\n\n- P1 task\n\n"
            "## Technical Debt\n\n- P2 task\n\n"
            "## Strategic Goals\n\n- P3 task\n"
        )

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)

            research = DeepResearch(
                research_env["instance"],
                research_env["project_name"],
                research_env["project_path"],
            )

            result = research.format_for_agent()

            assert "🔴" in result
            assert "🟡" in result
            assert "🟢" in result

    def test_limits_format_to_top_5(self, research_env):
        """Format truncates to 5 suggestions."""
        priorities_file = (
            research_env["instance"]
            / "memory"
            / "projects"
            / research_env["project_name"]
            / "priorities.md"
        )
        # 8 items — only 5 should be formatted
        items = "\n".join(f"- Item {i}" for i in range(8))
        priorities_file.write_text(f"## Current Focus\n\n{items}\n")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)

            research = DeepResearch(
                research_env["instance"],
                research_env["project_name"],
                research_env["project_path"],
            )

            result = research.format_for_agent()

            # Count numbered items in output
            numbered = [line for line in result.split("\n") if line.strip().startswith(("1.", "2.", "3.", "4.", "5.", "6.", "7.", "8."))]
            assert len(numbered) == 5

    def test_no_suggestions_fallback_message(self, research_env):
        """Shows fallback message when no suggestions and only do_not_touch."""
        priorities_file = (
            research_env["instance"]
            / "memory"
            / "projects"
            / research_env["project_name"]
            / "priorities.md"
        )
        priorities_file.write_text("## Do Not Touch\n\n- Fragile module\n")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)

            research = DeepResearch(
                research_env["instance"],
                research_env["project_name"],
                research_env["project_path"],
            )

            result = research.format_for_agent()

            assert "No specific suggestions" in result
            assert "Fragile module" in result

    def test_staleness_only_produces_output(self, research_env):
        """Staleness warning alone is enough to produce output."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)

            with patch("app.session_tracker.get_staleness_warning", return_value="⚠️ Warning"):
                research = DeepResearch(
                    research_env["instance"],
                    research_env["project_name"],
                    research_env["project_path"],
                )

                result = research.format_for_agent()

                assert result != ""
                assert "⚠️ Warning" in result


# ---------------------------------------------------------------------------
# get_pending_prs error handling
# ---------------------------------------------------------------------------

class TestGetPendingPrsEdgeCases:
    """Edge cases for PR fetching."""

    def test_gh_not_available(self, research_env):
        """Returns empty list when gh CLI is not available."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError()

            research = DeepResearch(
                research_env["instance"],
                research_env["project_name"],
                research_env["project_path"],
            )

            result = research.get_pending_prs()

            assert result == []

    def test_gh_timeout(self, research_env):
        """Returns empty list on timeout."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="gh", timeout=30)

            research = DeepResearch(
                research_env["instance"],
                research_env["project_name"],
                research_env["project_path"],
            )

            result = research.get_pending_prs()

            assert result == []

    def test_gh_returns_invalid_json(self, research_env):
        """Returns empty list on invalid JSON."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="not valid json",
            )

            research = DeepResearch(
                research_env["instance"],
                research_env["project_name"],
                research_env["project_path"],
            )

            result = research.get_pending_prs()

            assert result == []


# ---------------------------------------------------------------------------
# get_open_issues edge cases
# ---------------------------------------------------------------------------

class TestGetOpenIssuesEdgeCases:
    """Additional edge cases for issue fetching."""

    def test_custom_limit(self, research_env):
        """Custom limit is passed to gh CLI."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="[]",
            )

            research = DeepResearch(
                research_env["instance"],
                research_env["project_name"],
                research_env["project_path"],
            )

            research.get_open_issues(limit=3)

            cmd = mock_run.call_args[0][0]
            assert "--limit" in cmd
            idx = cmd.index("--limit")
            assert cmd[idx + 1] == "3"

    def test_invalid_json_returns_empty(self, research_env):
        """Returns empty list when gh returns invalid JSON."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="{broken json",
            )

            research = DeepResearch(
                research_env["instance"],
                research_env["project_name"],
                research_env["project_path"],
            )

            result = research.get_open_issues()

            assert result == []


# ---------------------------------------------------------------------------
# get_recent_journal_topics edge cases
# ---------------------------------------------------------------------------

class TestGetRecentJournalTopicsEdgeCases:
    """Edge cases for journal topic extraction."""

    def test_multiple_days(self, research_env):
        """Extracts topics from multiple day files."""
        from datetime import timedelta

        for i in range(3):
            date = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            journal_dir = research_env["instance"] / "journal" / date
            journal_dir.mkdir(parents=True)
            journal_file = journal_dir / f"{research_env['project_name']}.md"
            journal_file.write_text(f"## Session — Day {i} work\n")

        research = DeepResearch(
            research_env["instance"],
            research_env["project_name"],
            research_env["project_path"],
        )

        result = research.get_recent_journal_topics(days=3)

        assert len(result) == 3
        assert "Session — Day 0 work" in result
        assert "Session — Day 2 work" in result

    def test_custom_days_parameter(self, research_env):
        """Respects the days parameter."""
        from datetime import timedelta

        # Create entries for 5 days
        for i in range(5):
            date = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            journal_dir = research_env["instance"] / "journal" / date
            journal_dir.mkdir(parents=True)
            journal_file = journal_dir / f"{research_env['project_name']}.md"
            journal_file.write_text(f"## Day {i}\n")

        research = DeepResearch(
            research_env["instance"],
            research_env["project_name"],
            research_env["project_path"],
        )

        # Only look at last 2 days
        result = research.get_recent_journal_topics(days=2)

        assert len(result) == 2


# ---------------------------------------------------------------------------
# get_priorities edge cases
# ---------------------------------------------------------------------------

class TestGetPrioritiesEdgeCases:
    """Additional edge cases for priorities parsing."""

    def test_empty_dash_items_skipped(self, research_env):
        """Items that are just '- ' (dash + space) are skipped."""
        priorities_file = (
            research_env["instance"]
            / "memory"
            / "projects"
            / research_env["project_name"]
            / "priorities.md"
        )
        priorities_file.write_text("## Current Focus\n\n- \n- Real task\n- \n")

        research = DeepResearch(
            research_env["instance"],
            research_env["project_name"],
            research_env["project_path"],
        )

        result = research.get_priorities()

        assert len(result["current_focus"]) == 1
        assert result["current_focus"][0] == "Real task"

    def test_notes_placeholder_skipped(self, research_env):
        """Notes section with placeholder is returned as empty."""
        priorities_file = (
            research_env["instance"]
            / "memory"
            / "projects"
            / research_env["project_name"]
            / "priorities.md"
        )
        priorities_file.write_text("## Notes\n\n(Add context here)\n")

        research = DeepResearch(
            research_env["instance"],
            research_env["project_name"],
            research_env["project_path"],
        )

        result = research.get_priorities()

        assert result["notes"] == ""

    def test_case_insensitive_section_headers(self, research_env):
        """Section headers are matched case-insensitively."""
        priorities_file = (
            research_env["instance"]
            / "memory"
            / "projects"
            / research_env["project_name"]
            / "priorities.md"
        )
        priorities_file.write_text("## current focus\n\n- Task A\n\n## TECHNICAL DEBT\n\n- Task B\n")

        research = DeepResearch(
            research_env["instance"],
            research_env["project_name"],
            research_env["project_path"],
        )

        result = research.get_priorities()

        assert "Task A" in result["current_focus"]
        assert "Task B" in result["technical_debt"]


# ---------------------------------------------------------------------------
# CLI entry point (main)
# ---------------------------------------------------------------------------

class TestMainCLI:
    """Tests for the CLI entry point."""

    def test_main_markdown_output(self, research_env, capsys):
        """CLI outputs markdown by default."""
        priorities_file = (
            research_env["instance"]
            / "memory"
            / "projects"
            / research_env["project_name"]
            / "priorities.md"
        )
        priorities_file.write_text("## Current Focus\n\n- CLI test task\n")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)

            with patch("sys.argv", [
                "deep_research.py",
                str(research_env["instance"]),
                research_env["project_name"],
                str(research_env["project_path"]),
            ]):
                from app.deep_research import main
                main()

        captured = capsys.readouterr()
        assert "Deep Research Suggestions" in captured.out
        assert "CLI test task" in captured.out

    def test_main_json_output(self, research_env, capsys):
        """CLI with --json outputs valid JSON."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)

            with patch("sys.argv", [
                "deep_research.py",
                str(research_env["instance"]),
                research_env["project_name"],
                str(research_env["project_path"]),
                "--json",
            ]):
                from app.deep_research import main
                main()

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "priorities" in data
        assert "suggestions" in data

    def test_main_insufficient_args(self):
        """CLI exits with error when args are missing."""
        with patch("sys.argv", ["deep_research.py"]):
            from app.deep_research import main
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# Constructor and path resolution
# ---------------------------------------------------------------------------

class TestConstructor:
    """Tests for DeepResearch initialization."""

    def test_memory_dir_path(self, research_env):
        """Memory dir is correctly computed from instance + project name."""
        research = DeepResearch(
            research_env["instance"],
            research_env["project_name"],
            research_env["project_path"],
        )

        expected = research_env["instance"] / "memory" / "projects" / research_env["project_name"]
        assert research.memory_dir == expected

    def test_stores_project_path(self, research_env):
        """Project path is stored for gh CLI cwd."""
        research = DeepResearch(
            research_env["instance"],
            research_env["project_name"],
            research_env["project_path"],
        )

        assert research.project_path == research_env["project_path"]


class TestExtractIssueNumbers:
    """Tests for _extract_issue_numbers() helper."""

    def test_single_issue(self):
        assert _extract_issue_numbers("GitHub #42: Add feature") == {42}

    def test_multiple_issues(self):
        assert _extract_issue_numbers("Fixes #10 and #20") == {10, 20}

    def test_no_issues(self):
        assert _extract_issue_numbers("No issues here") == set()

    def test_issue_in_branch_name(self):
        assert _extract_issue_numbers("koan/implement-#1042") == {1042}

    def test_empty_string(self):
        assert _extract_issue_numbers("") == set()


class TestNormalizeTokens:
    """Tests for _normalize_tokens() helper."""

    def test_basic_tokens(self):
        tokens = _normalize_tokens("Add passive mode toggle")
        assert "passive" in tokens
        assert "mode" in tokens  # 4 chars, passes >= 3 filter
        assert "toggle" in tokens

    def test_stops_removed(self):
        tokens = _normalize_tokens("fix the broken implementation")
        assert "the" not in tokens
        assert "fix" not in tokens  # stop word
        assert "broken" in tokens
        assert "implementation" in tokens

    def test_short_words_excluded(self):
        tokens = _normalize_tokens("a to of CI PR")
        # All under 3 chars
        assert len(tokens) == 0

    def test_empty_string(self):
        assert _normalize_tokens("") == set()


class TestPrCoverage:
    """Tests for PR coverage detection in topic selection."""

    def _make_research(self, research_env, pending_prs=None):
        """Create a DeepResearch instance with mocked PRs."""
        research = DeepResearch(
            research_env["instance"],
            research_env["project_name"],
            research_env["project_path"],
        )
        research._pending_prs = pending_prs or []
        return research

    def test_build_pr_coverage_extracts_issue_numbers(self, research_env):
        """Issue numbers from PR titles and branches are collected."""
        research = self._make_research(research_env, [
            {"number": 100, "title": "feat: implement #42", "headRefName": "koan/implement-42"},
            {"number": 101, "title": "fix: resolve #99", "headRefName": "koan/fix-bug"},
        ])

        coverage = research._build_pr_coverage()

        assert 42 in coverage["issue_numbers"]
        assert 99 in coverage["issue_numbers"]

    def test_build_pr_coverage_branch_pattern_extraction(self, research_env):
        """Issue numbers from implement-NNN branch patterns are extracted."""
        research = self._make_research(research_env, [
            {"number": 200, "title": "some feature", "headRefName": "koan/implement-1042"},
        ])

        coverage = research._build_pr_coverage()

        assert 1042 in coverage["issue_numbers"]

    def test_build_pr_coverage_fix_branch_pattern(self, research_env):
        """Issue numbers from fix-NNN branch patterns are extracted."""
        research = self._make_research(research_env, [
            {"number": 300, "title": "bug fix", "headRefName": "koan/fix-555"},
        ])

        coverage = research._build_pr_coverage()

        assert 555 in coverage["issue_numbers"]

    def test_topic_has_open_pr_issue_match(self, research_env):
        """Topic referencing an issue covered by a PR is detected."""
        research = self._make_research(research_env, [
            {"number": 100, "title": "feat: add passive mode (#1042)", "headRefName": "koan/implement-1042"},
        ])

        coverage = research._build_pr_coverage()
        result = research._topic_has_open_pr("GitHub #1042: Add passive mode", coverage)

        assert result == 100

    def test_topic_has_open_pr_no_match(self, research_env):
        """Topic without matching PR returns None."""
        research = self._make_research(research_env, [
            {"number": 100, "title": "feat: something else", "headRefName": "koan/other"},
        ])

        coverage = research._build_pr_coverage()
        result = research._topic_has_open_pr("GitHub #999: Unrelated feature", coverage)

        assert result is None

    def test_topic_has_open_pr_token_match(self, research_env):
        """Topic with significant token overlap is detected."""
        research = self._make_research(research_env, [
            {"number": 200, "title": "feat: add passive active mode toggle", "headRefName": "koan/passive-mode"},
        ])

        coverage = research._build_pr_coverage()
        result = research._topic_has_open_pr(
            "Add 'mode' option: active (default) vs passive", coverage
        )

        assert result == 200

    def test_topic_has_open_pr_few_tokens_no_false_positive(self, research_env):
        """Short topics don't false-positive on fuzzy match."""
        research = self._make_research(research_env, [
            {"number": 300, "title": "feat: security audit improvements", "headRefName": "koan/security"},
        ])

        coverage = research._build_pr_coverage()
        # Only 1 overlapping meaningful token — should not match
        result = research._topic_has_open_pr("Security review", coverage)

        assert result is None

    def test_suggest_topics_filters_covered_issues(self, research_env):
        """Issues already covered by open PRs are excluded from suggestions."""
        # Write a priorities file with nothing (so only issues show up)
        mem_dir = research_env["instance"] / "memory" / "projects" / research_env["project_name"]
        (mem_dir / "priorities.md").write_text("# Priorities\n\n## Current Focus\n\n## Technical Debt\n")

        research = self._make_research(research_env, [
            {"number": 100, "title": "feat: implement #42", "headRefName": "koan/implement-42"},
        ])

        # Mock GitHub issues to include #42 and #99
        mock_issues = [
            {"number": 42, "title": "Add feature X", "labels": [], "createdAt": "2026-01-01"},
            {"number": 99, "title": "Add feature Y", "labels": [], "createdAt": "2026-01-02"},
        ]

        with patch.object(research, "get_open_issues", return_value=mock_issues), \
             patch.object(research, "get_pr_feedback", return_value={"alignment_summary": "", "category_boosts": {}}):
            suggestions = research.suggest_topics()

        topics = [s["topic"] for s in suggestions]
        # #42 should be filtered out (covered by PR #100)
        assert not any("#42" in t for t in topics)
        # #99 should still be present
        assert any("#99" in t for t in topics)

    def test_format_includes_in_flight_section(self, research_env):
        """format_for_agent() includes In-Flight Work section with open PRs."""
        research = self._make_research(research_env, [
            {"number": 100, "title": "feat: add passive mode", "headRefName": "koan/passive", "createdAt": "2026-01-01"},
            {"number": 101, "title": "fix: startup race", "headRefName": "koan/fix-startup", "createdAt": "2026-01-02"},
        ])

        with patch.object(research, "suggest_topics", return_value=[
            {"topic": "Some work", "source": "test", "reasoning": "test", "priority": 1},
        ]), \
             patch.object(research, "get_do_not_touch", return_value=[]), \
             patch.object(research, "get_staleness_warning", return_value=""), \
             patch.object(research, "get_pr_feedback", return_value={"alignment_summary": "", "category_boosts": {}}):
            output = research.format_for_agent()

        assert "In-Flight Work" in output
        assert "PR #100" in output
        assert "PR #101" in output

    def test_format_no_prs_no_in_flight_section(self, research_env):
        """format_for_agent() omits In-Flight Work section when no PRs exist."""
        research = self._make_research(research_env, [])

        with patch.object(research, "suggest_topics", return_value=[
            {"topic": "Some work", "source": "test", "reasoning": "test", "priority": 1},
        ]), \
             patch.object(research, "get_do_not_touch", return_value=[]), \
             patch.object(research, "get_staleness_warning", return_value=""), \
             patch.object(research, "get_pr_feedback", return_value={"alignment_summary": "", "category_boosts": {}}):
            output = research.format_for_agent()

        assert "In-Flight Work" not in output

    def test_pending_prs_cached(self, research_env):
        """get_pending_prs() caches results across calls."""
        research = DeepResearch(
            research_env["instance"],
            research_env["project_name"],
            research_env["project_path"],
        )
        # Reset the cache set by autouse fixture
        research._pending_prs = None

        mock_prs = [{"number": 1, "title": "test", "headRefName": "koan/test", "createdAt": "2026-01-01"}]

        with patch("app.github.run_gh", return_value=json.dumps(mock_prs)):
            result1 = research.get_pending_prs()

        # Second call should use cache, not call run_gh again
        with patch("app.github.run_gh", side_effect=RuntimeError("should not be called")):
            result2 = research.get_pending_prs()

        assert result1 == result2 == mock_prs

    def test_to_json_includes_pending_prs(self, research_env):
        """to_json() includes pending_prs in output."""
        research = self._make_research(research_env, [
            {"number": 42, "title": "test PR", "headRefName": "koan/test", "createdAt": "2026-01-01"},
        ])

        with patch.object(research, "suggest_topics", return_value=[]), \
             patch.object(research, "get_priorities", return_value={
                 "current_focus": [], "strategic_goals": [],
                 "technical_debt": [], "do_not_touch": [], "notes": "",
             }), \
             patch.object(research, "get_open_issues", return_value=[]), \
             patch.object(research, "get_recent_journal_topics", return_value=[]), \
             patch.object(research, "get_pr_feedback", return_value={"alignment_summary": "", "category_boosts": {}}):
            data = json.loads(research.to_json())

        assert "pending_prs" in data
        assert data["pending_prs"][0]["number"] == 42
