"""Tests for the /changelog core skill — changelog generation."""

import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from app.skills import SkillContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx(tmp_path, args="", command_name="changelog"):
    """Create a SkillContext for /changelog."""
    instance_dir = tmp_path / "instance"
    instance_dir.mkdir(exist_ok=True)
    return SkillContext(
        koan_root=tmp_path,
        instance_dir=instance_dir,
        command_name=command_name,
        args=args,
    )


def _make_journal(instance_dir, project, date_str, content):
    """Create a journal entry for a project on a given date."""
    journal_dir = instance_dir / "journal" / date_str
    journal_dir.mkdir(parents=True, exist_ok=True)
    (journal_dir / f"{project}.md").write_text(content)


def _mock_git_log(commits):
    """Build a mock subprocess result with git log output.

    Args:
        commits: List of (hash, message) tuples.
    """
    output = "\n".join(f"{h} {m}" for h, m in commits)

    def side_effect(cmd, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout=output, stderr=""
        )

    return side_effect


def _mock_git_log_empty():
    """Return mock for empty git log."""
    def side_effect(cmd, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr=""
        )

    return side_effect


def _mock_git_log_error():
    """Return mock for git log failure."""
    def side_effect(cmd, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd, returncode=128, stdout="", stderr="not a git repo"
        )

    return side_effect


SAMPLE_COMMITS = [
    ("abc1234", "feat: add user authentication"),
    ("def5678", "fix: resolve login timeout issue"),
    ("ghi9012", "docs: update API reference"),
    ("jkl3456", "perf: optimize database queries"),
    ("mno7890", "refactor: extract validation logic"),
    ("pqr1234", "chore: update dependencies"),
    ("stu5678", "feat(auth): add OAuth2 support"),
    ("vwx9012", "fix(api): handle null response"),
    ("yza3456", "test: add integration tests"),
]


# ---------------------------------------------------------------------------
# Tests: Argument parsing
# ---------------------------------------------------------------------------

class TestParseArgs:
    def test_no_args(self):
        from skills.core.changelog.handler import _parse_args
        project, since, fmt = _parse_args("")
        assert project == ""
        assert fmt == "telegram"
        # Default is 7 days ago
        expected = datetime.now() - timedelta(days=7)
        assert abs((since - expected).total_seconds()) < 5

    def test_project_only(self):
        from skills.core.changelog.handler import _parse_args
        project, since, fmt = _parse_args("myproject")
        assert project == "myproject"
        assert fmt == "telegram"

    def test_since_flag(self):
        from skills.core.changelog.handler import _parse_args
        project, since, fmt = _parse_args("--since=2026-01-15")
        assert project == ""
        assert since == datetime(2026, 1, 15)

    def test_format_md(self):
        from skills.core.changelog.handler import _parse_args
        project, since, fmt = _parse_args("--format=md")
        assert fmt == "md"

    def test_format_markdown(self):
        from skills.core.changelog.handler import _parse_args
        project, since, fmt = _parse_args("--format=markdown")
        assert fmt == "md"

    def test_format_telegram(self):
        from skills.core.changelog.handler import _parse_args
        project, since, fmt = _parse_args("--format=telegram")
        assert fmt == "telegram"

    def test_all_args(self):
        from skills.core.changelog.handler import _parse_args
        project, since, fmt = _parse_args("koan --since=2026-03-01 --format=md")
        assert project == "koan"
        assert since == datetime(2026, 3, 1)
        assert fmt == "md"

    def test_invalid_date_uses_default(self):
        from skills.core.changelog.handler import _parse_args
        project, since, fmt = _parse_args("--since=not-a-date")
        expected = datetime.now() - timedelta(days=7)
        assert abs((since - expected).total_seconds()) < 5

    def test_project_before_flags(self):
        from skills.core.changelog.handler import _parse_args
        project, since, fmt = _parse_args("myproject --format=md")
        assert project == "myproject"
        assert fmt == "md"


# ---------------------------------------------------------------------------
# Tests: Commit classification
# ---------------------------------------------------------------------------

class TestClassifyCommit:
    def test_feat_prefix(self):
        from skills.core.changelog.handler import _classify_commit
        section, desc = _classify_commit("feat: add new feature")
        assert section == "Added"
        assert desc == "add new feature"

    def test_fix_prefix(self):
        from skills.core.changelog.handler import _classify_commit
        section, desc = _classify_commit("fix: resolve crash on startup")
        assert section == "Fixed"
        assert desc == "resolve crash on startup"

    def test_docs_prefix(self):
        from skills.core.changelog.handler import _classify_commit
        section, desc = _classify_commit("docs: update readme")
        assert section == "Documentation"
        assert desc == "update readme"

    def test_perf_prefix(self):
        from skills.core.changelog.handler import _classify_commit
        section, desc = _classify_commit("perf: optimize queries")
        assert section == "Performance"
        assert desc == "optimize queries"

    def test_refactor_prefix(self):
        from skills.core.changelog.handler import _classify_commit
        section, desc = _classify_commit("refactor: extract method")
        assert section == "Changed"
        assert desc == "extract method"

    def test_chore_prefix(self):
        from skills.core.changelog.handler import _classify_commit
        section, desc = _classify_commit("chore: update deps")
        assert section == "Other"
        assert desc == "update deps"

    def test_scoped_prefix(self):
        from skills.core.changelog.handler import _classify_commit
        section, desc = _classify_commit("feat(auth): add OAuth2")
        assert section == "Added"
        assert desc == "add OAuth2"

    def test_breaking_change(self):
        from skills.core.changelog.handler import _classify_commit
        section, desc = _classify_commit("feat!: breaking API change")
        assert section == "Added"
        assert desc == "breaking API change"

    def test_revert_prefix(self):
        from skills.core.changelog.handler import _classify_commit
        section, desc = _classify_commit("revert: undo feature X")
        assert section == "Removed"
        assert desc == "undo feature X"

    def test_keyword_add(self):
        from skills.core.changelog.handler import _classify_commit
        section, desc = _classify_commit("add user management page")
        assert section == "Added"
        assert desc == "add user management page"

    def test_keyword_fix(self):
        from skills.core.changelog.handler import _classify_commit
        section, desc = _classify_commit("fix login timeout issue")
        assert section == "Fixed"

    def test_keyword_remove(self):
        from skills.core.changelog.handler import _classify_commit
        section, desc = _classify_commit("remove deprecated API")
        assert section == "Removed"

    def test_keyword_update(self):
        from skills.core.changelog.handler import _classify_commit
        section, desc = _classify_commit("update configuration system")
        assert section == "Changed"

    def test_keyword_optimize(self):
        from skills.core.changelog.handler import _classify_commit
        section, desc = _classify_commit("optimize database connection pool")
        assert section == "Performance"

    def test_keyword_docs(self):
        from skills.core.changelog.handler import _classify_commit
        section, desc = _classify_commit("document the configuration options")
        assert section == "Documentation"

    def test_no_match_is_other(self):
        from skills.core.changelog.handler import _classify_commit
        section, desc = _classify_commit("bump version to 2.0")
        assert section == "Other"
        assert desc == "bump version to 2.0"

    def test_test_prefix(self):
        from skills.core.changelog.handler import _classify_commit
        section, desc = _classify_commit("test: add unit tests")
        assert section == "Other"
        assert desc == "add unit tests"

    def test_ci_prefix(self):
        from skills.core.changelog.handler import _classify_commit
        section, desc = _classify_commit("ci: fix pipeline")
        assert section == "Other"
        assert desc == "fix pipeline"

    def test_style_prefix(self):
        from skills.core.changelog.handler import _classify_commit
        section, desc = _classify_commit("style: format code")
        assert section == "Changed"
        assert desc == "format code"

    def test_build_prefix(self):
        from skills.core.changelog.handler import _classify_commit
        section, desc = _classify_commit("build: update webpack config")
        assert section == "Other"
        assert desc == "update webpack config"


# ---------------------------------------------------------------------------
# Tests: Commit categorization (batch)
# ---------------------------------------------------------------------------

class TestCategorizeCommits:
    def test_groups_by_section(self):
        from skills.core.changelog.handler import _categorize_commits
        commits = [
            ("a1", "feat: feature A"),
            ("a2", "feat: feature B"),
            ("b1", "fix: bug fix"),
            ("c1", "docs: update docs"),
        ]
        sections = _categorize_commits(commits)
        assert len(sections["Added"]) == 2
        assert len(sections["Fixed"]) == 1
        assert len(sections["Documentation"]) == 1

    def test_empty_commits(self):
        from skills.core.changelog.handler import _categorize_commits
        sections = _categorize_commits([])
        assert sections == {}

    def test_all_same_category(self):
        from skills.core.changelog.handler import _categorize_commits
        commits = [
            ("a1", "fix: bug 1"),
            ("a2", "fix: bug 2"),
            ("a3", "fix: bug 3"),
        ]
        sections = _categorize_commits(commits)
        assert len(sections) == 1
        assert len(sections["Fixed"]) == 3


# ---------------------------------------------------------------------------
# Tests: Git log fetching
# ---------------------------------------------------------------------------

class TestGetCommits:
    def test_parses_git_output(self):
        from skills.core.changelog.handler import _get_commits
        mock = _mock_git_log([("abc", "feat: new thing"), ("def", "fix: old bug")])
        with patch("skills.core.changelog.handler.subprocess.run", side_effect=mock):
            commits = _get_commits("/fake/path", datetime.now() - timedelta(days=7))
        assert len(commits) == 2
        assert commits[0] == ("abc", "feat: new thing")

    def test_empty_log(self):
        from skills.core.changelog.handler import _get_commits
        mock = _mock_git_log_empty()
        with patch("skills.core.changelog.handler.subprocess.run", side_effect=mock):
            commits = _get_commits("/fake/path", datetime.now())
        assert commits == []

    def test_git_error(self):
        from skills.core.changelog.handler import _get_commits
        mock = _mock_git_log_error()
        with patch("skills.core.changelog.handler.subprocess.run", side_effect=mock):
            commits = _get_commits("/fake/path", datetime.now())
        assert commits == []

    def test_timeout_returns_empty(self):
        from skills.core.changelog.handler import _get_commits

        def timeout_side_effect(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 10)

        with patch("skills.core.changelog.handler.subprocess.run", side_effect=timeout_side_effect):
            commits = _get_commits("/fake/path", datetime.now())
        assert commits == []

    def test_os_error_returns_empty(self):
        from skills.core.changelog.handler import _get_commits

        def os_error(cmd, **kwargs):
            raise OSError("no such command")

        with patch("skills.core.changelog.handler.subprocess.run", side_effect=os_error):
            commits = _get_commits("/fake/path", datetime.now())
        assert commits == []


# ---------------------------------------------------------------------------
# Tests: Journal entries
# ---------------------------------------------------------------------------

class TestJournalEntries:
    def test_reads_project_journal(self, tmp_path):
        from skills.core.changelog.handler import _get_journal_entries
        instance_dir = tmp_path / "instance"
        today = datetime.now().strftime("%Y-%m-%d")
        _make_journal(instance_dir, "myproject", today,
                      "# Journal\nImplemented new auth flow\nShort\nRefactored the database layer for performance")
        entries = _get_journal_entries(instance_dir, "myproject",
                                       datetime.now() - timedelta(days=1))
        # Skips header and lines <= 10 chars
        assert len(entries) == 2
        assert "auth flow" in entries[0]

    def test_no_journal_dir(self, tmp_path):
        from skills.core.changelog.handler import _get_journal_entries
        instance_dir = tmp_path / "instance"
        instance_dir.mkdir()
        entries = _get_journal_entries(instance_dir, "project",
                                       datetime.now() - timedelta(days=1))
        assert entries == []

    def test_no_matching_project(self, tmp_path):
        from skills.core.changelog.handler import _get_journal_entries
        instance_dir = tmp_path / "instance"
        today = datetime.now().strftime("%Y-%m-%d")
        _make_journal(instance_dir, "other", today, "Some content here that is long enough")
        entries = _get_journal_entries(instance_dir, "myproject",
                                       datetime.now() - timedelta(days=1))
        assert entries == []

    def test_multiple_days(self, tmp_path):
        from skills.core.changelog.handler import _get_journal_entries
        instance_dir = tmp_path / "instance"
        today = datetime.now()
        yesterday = today - timedelta(days=1)

        _make_journal(instance_dir, "proj", today.strftime("%Y-%m-%d"),
                      "Today's work was very productive")
        _make_journal(instance_dir, "proj", yesterday.strftime("%Y-%m-%d"),
                      "Yesterday's work was also good")

        entries = _get_journal_entries(instance_dir, "proj",
                                       today - timedelta(days=2))
        assert len(entries) == 2

    def test_skips_short_lines(self, tmp_path):
        from skills.core.changelog.handler import _get_journal_entries
        instance_dir = tmp_path / "instance"
        today = datetime.now().strftime("%Y-%m-%d")
        _make_journal(instance_dir, "proj", today, "Short\nThis line is long enough to be included")
        entries = _get_journal_entries(instance_dir, "proj",
                                       datetime.now() - timedelta(days=1))
        assert len(entries) == 1
        assert "long enough" in entries[0]

    def test_skips_headers(self, tmp_path):
        from skills.core.changelog.handler import _get_journal_entries
        instance_dir = tmp_path / "instance"
        today = datetime.now().strftime("%Y-%m-%d")
        _make_journal(instance_dir, "proj", today,
                      "# Header\n## Subheader\nActual content that should be included")
        entries = _get_journal_entries(instance_dir, "proj",
                                       datetime.now() - timedelta(days=1))
        assert len(entries) == 1
        assert entries[0].startswith("Actual content")


# ---------------------------------------------------------------------------
# Tests: Markdown formatting
# ---------------------------------------------------------------------------

class TestFormatMarkdown:
    def test_basic_output(self):
        from skills.core.changelog.handler import _format_markdown
        sections = {
            "Added": [("abc", "new feature")],
            "Fixed": [("def", "bug fix")],
        }
        result = _format_markdown("myproject", datetime(2026, 3, 1), sections, [])
        assert "# Changelog" in result
        assert "myproject" in result
        assert "### Added" in result
        assert "### Fixed" in result
        assert "- new feature (abc)" in result
        assert "- bug fix (def)" in result
        assert "2 commits" in result

    def test_includes_journal(self):
        from skills.core.changelog.handler import _format_markdown
        sections = {"Added": [("abc", "feature")]}
        journal = ["Worked on authentication improvements"]
        result = _format_markdown("proj", datetime(2026, 3, 1), sections, journal)
        assert "Context (from journal)" in result
        assert "authentication improvements" in result

    def test_section_ordering(self):
        from skills.core.changelog.handler import _format_markdown
        sections = {
            "Other": [("a", "other")],
            "Added": [("b", "feature")],
            "Fixed": [("c", "fix")],
        }
        result = _format_markdown("proj", datetime(2026, 3, 1), sections, [])
        added_pos = result.index("### Added")
        fixed_pos = result.index("### Fixed")
        other_pos = result.index("### Other")
        assert added_pos < fixed_pos < other_pos

    def test_empty_sections_skipped(self):
        from skills.core.changelog.handler import _format_markdown
        sections = {"Added": [("a", "feature")]}
        result = _format_markdown("proj", datetime(2026, 3, 1), sections, [])
        assert "### Fixed" not in result
        assert "### Removed" not in result

    def test_journal_truncation(self):
        from skills.core.changelog.handler import _format_markdown
        long_entry = "x" * 200
        result = _format_markdown("proj", datetime(2026, 3, 1),
                                  {"Added": [("a", "feat")]},
                                  [long_entry])
        # Should be truncated
        for line in result.splitlines():
            if line.startswith("- x"):
                assert len(line) < 200
                assert line.endswith("...")

    def test_journal_max_entries(self):
        from skills.core.changelog.handler import _format_markdown
        entries = [f"Journal entry number {i} with enough length" for i in range(20)]
        result = _format_markdown("proj", datetime(2026, 3, 1),
                                  {"Added": [("a", "feat")]},
                                  entries)
        # Max 10 journal entries
        journal_lines = [l for l in result.splitlines()
                        if l.startswith("- Journal entry")]
        assert len(journal_lines) == 10

    def test_since_date_in_header(self):
        from skills.core.changelog.handler import _format_markdown
        result = _format_markdown("proj", datetime(2026, 3, 1),
                                  {"Added": [("a", "feat")]}, [])
        assert "2026-03-01" in result


# ---------------------------------------------------------------------------
# Tests: Telegram formatting
# ---------------------------------------------------------------------------

class TestFormatTelegram:
    def test_basic_output(self):
        from skills.core.changelog.handler import _format_telegram
        sections = {
            "Added": [("abc", "new feature")],
            "Fixed": [("def", "bug fix")],
        }
        result = _format_telegram("myproject", datetime(2026, 3, 1), sections, [])
        assert "Changelog: myproject" in result
        assert "2 commits" in result
        assert "Added (1):" in result
        assert "Fixed (1):" in result

    def test_truncates_long_descriptions(self):
        from skills.core.changelog.handler import _format_telegram
        long_desc = "x" * 100
        sections = {"Added": [("abc", long_desc)]}
        result = _format_telegram("proj", datetime(2026, 3, 1), sections, [])
        for line in result.splitlines():
            if line.strip().startswith("+"):
                assert len(line) < 100
                assert "..." in line

    def test_limits_items_per_section(self):
        from skills.core.changelog.handler import _format_telegram
        items = [(f"h{i}", f"feature {i}") for i in range(10)]
        sections = {"Added": items}
        result = _format_telegram("proj", datetime(2026, 3, 1), sections, [])
        assert "and 5 more" in result

    def test_includes_journal_context(self):
        from skills.core.changelog.handler import _format_telegram
        sections = {"Added": [("a", "feature")]}
        journal = ["Implemented new user flow with care"]
        result = _format_telegram("proj", datetime(2026, 3, 1), sections, journal)
        assert "Context:" in result
        assert "user flow" in result

    def test_section_icons(self):
        from skills.core.changelog.handler import _format_telegram
        sections = {
            "Added": [("a", "feature")],
            "Fixed": [("b", "fix")],
        }
        result = _format_telegram("proj", datetime(2026, 3, 1), sections, [])
        assert "+ feature" in result
        assert "! fix" in result


# ---------------------------------------------------------------------------
# Tests: Project resolution
# ---------------------------------------------------------------------------

class TestResolveProject:
    def test_named_project_found(self, tmp_path):
        from skills.core.changelog.handler import _resolve_project
        ctx = _make_ctx(tmp_path)
        with patch("app.utils.get_known_projects",
                   return_value=[("myproject", "/path/to/project")]):
            result = _resolve_project(ctx, "myproject")
        assert result == "/path/to/project"

    def test_named_project_case_insensitive(self, tmp_path):
        from skills.core.changelog.handler import _resolve_project
        ctx = _make_ctx(tmp_path)
        with patch("app.utils.get_known_projects",
                   return_value=[("MyProject", "/path/to/project")]):
            result = _resolve_project(ctx, "myproject")
        assert result == "/path/to/project"

    def test_named_project_not_found(self, tmp_path):
        from skills.core.changelog.handler import _resolve_project
        ctx = _make_ctx(tmp_path)
        with patch("app.utils.get_known_projects",
                   return_value=[("other", "/path/to/other")]):
            result = _resolve_project(ctx, "myproject")
        assert result is None

    def test_no_project_single_known(self, tmp_path):
        from skills.core.changelog.handler import _resolve_project
        ctx = _make_ctx(tmp_path)
        with patch("app.utils.get_known_projects",
                   return_value=[("solo", "/path/to/solo")]):
            result = _resolve_project(ctx, "")
        assert result == "/path/to/solo"

    def test_no_project_multiple_known(self, tmp_path):
        from skills.core.changelog.handler import _resolve_project
        ctx = _make_ctx(tmp_path)
        with patch("app.utils.get_known_projects",
                   return_value=[("a", "/a"), ("b", "/b")]):
            result = _resolve_project(ctx, "")
        assert result is None

    def test_no_known_projects(self, tmp_path):
        from skills.core.changelog.handler import _resolve_project
        ctx = _make_ctx(tmp_path)
        with patch("app.utils.get_known_projects",
                   return_value=[]):
            result = _resolve_project(ctx, "")
        assert result is None


# ---------------------------------------------------------------------------
# Tests: Full handle() integration
# ---------------------------------------------------------------------------

class TestHandle:
    def test_no_project_no_args(self, tmp_path):
        from skills.core.changelog.handler import handle
        ctx = _make_ctx(tmp_path)
        with patch("app.utils.get_known_projects",
                   return_value=[]):
            result = handle(ctx)
        assert "not found" in result or "No project" in result

    def test_project_not_found(self, tmp_path):
        from skills.core.changelog.handler import handle
        ctx = _make_ctx(tmp_path, args="nonexistent")
        with patch("app.utils.get_known_projects",
                   return_value=[("other", "/other")]):
            result = handle(ctx)
        assert "not found" in result

    def test_no_commits(self, tmp_path):
        from skills.core.changelog.handler import handle
        ctx = _make_ctx(tmp_path, args="myproject")
        mock = _mock_git_log_empty()
        with patch("app.utils.get_known_projects",
                   return_value=[("myproject", "/path/to/project")]):
            with patch("skills.core.changelog.handler.subprocess.run",
                       side_effect=mock):
                result = handle(ctx)
        assert "No commits found" in result

    def test_generates_telegram_by_default(self, tmp_path):
        from skills.core.changelog.handler import handle
        ctx = _make_ctx(tmp_path, args="myproject")
        mock = _mock_git_log(SAMPLE_COMMITS)
        with patch("app.utils.get_known_projects",
                   return_value=[("myproject", "/path/to/project")]):
            with patch("skills.core.changelog.handler.subprocess.run",
                       side_effect=mock):
                result = handle(ctx)
        assert "Changelog: myproject" in result
        assert "commits" in result

    def test_generates_markdown(self, tmp_path):
        from skills.core.changelog.handler import handle
        ctx = _make_ctx(tmp_path, args="myproject --format=md")
        mock = _mock_git_log(SAMPLE_COMMITS)
        with patch("app.utils.get_known_projects",
                   return_value=[("myproject", "/path/to/project")]):
            with patch("skills.core.changelog.handler.subprocess.run",
                       side_effect=mock):
                result = handle(ctx)
        assert "# Changelog" in result
        assert "### Added" in result

    def test_with_since_date(self, tmp_path):
        from skills.core.changelog.handler import handle
        ctx = _make_ctx(tmp_path, args="myproject --since=2026-03-01")
        mock = _mock_git_log(SAMPLE_COMMITS)
        with patch("app.utils.get_known_projects",
                   return_value=[("myproject", "/path/to/project")]):
            with patch("skills.core.changelog.handler.subprocess.run",
                       side_effect=mock):
                result = handle(ctx)
        assert "Changelog: myproject" in result

    def test_single_project_auto_resolve(self, tmp_path):
        from skills.core.changelog.handler import handle
        ctx = _make_ctx(tmp_path)
        mock = _mock_git_log(SAMPLE_COMMITS[:2])
        with patch("app.utils.get_known_projects",
                   return_value=[("solo", "/path/to/solo")]):
            with patch("skills.core.changelog.handler.subprocess.run",
                       side_effect=mock):
                result = handle(ctx)
        assert "Changelog: solo" in result

    def test_with_journal_entries(self, tmp_path):
        from skills.core.changelog.handler import handle
        ctx = _make_ctx(tmp_path, args="myproject --format=md")
        today = datetime.now().strftime("%Y-%m-%d")
        _make_journal(ctx.instance_dir, "myproject", today,
                      "Worked on authentication improvements all day")
        mock = _mock_git_log(SAMPLE_COMMITS[:2])
        with patch("app.utils.get_known_projects",
                   return_value=[("myproject", "/path/to/project")]):
            with patch("skills.core.changelog.handler.subprocess.run",
                       side_effect=mock):
                result = handle(ctx)
        assert "Context (from journal)" in result
        assert "authentication" in result

    def test_changes_alias(self, tmp_path):
        from skills.core.changelog.handler import handle
        ctx = _make_ctx(tmp_path, args="myproject", command_name="changes")
        mock = _mock_git_log(SAMPLE_COMMITS[:1])
        with patch("app.utils.get_known_projects",
                   return_value=[("myproject", "/path/to/project")]):
            with patch("skills.core.changelog.handler.subprocess.run",
                       side_effect=mock):
                result = handle(ctx)
        assert "Changelog" in result
