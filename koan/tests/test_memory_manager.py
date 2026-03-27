"""Tests for memory_manager.py — scoped summary, compaction, learnings dedup, journal archival."""

import pytest
from datetime import date, timedelta
from unittest.mock import patch

from app.memory_manager import (
    MemoryManager,
    parse_summary_sessions,
    scoped_summary,
    compact_summary,
    cleanup_learnings,
    cap_learnings,
    archive_journals,
    run_cleanup,
    _extract_project_hint,
    _extract_session_digest,
    _balanced_select,
)


# ---------------------------------------------------------------------------
# _extract_project_hint
# ---------------------------------------------------------------------------

class TestExtractProjectHint:

    def test_parenthesized_french(self):
        assert _extract_project_hint("Session 1 (projet: koan) : blah") == "koan"

    def test_parenthesized_english(self):
        assert _extract_project_hint("Session 1 (project: koan) : blah") == "koan"

    def test_no_parens(self):
        assert _extract_project_hint("Session 1 projet:koan blah") == "koan"

    def test_case_insensitive(self):
        assert _extract_project_hint("Session 1 (Projet: Koan)") == "koan"

    def test_no_hint(self):
        assert _extract_project_hint("Session 1 : did some work") == ""

    def test_hyphenated_project(self):
        assert _extract_project_hint("(project: anantys-back)") == "anantys-back"


# ---------------------------------------------------------------------------
# parse_summary_sessions
# ---------------------------------------------------------------------------

class TestParseSummarySessions:

    def test_single_date_single_session(self):
        content = "# Summary\n\n## 2026-01-31\n\nSession 1 (projet: koan) : did stuff\n"
        sessions = parse_summary_sessions(content)
        assert len(sessions) == 1
        assert sessions[0][0] == "## 2026-01-31"
        assert "Session 1" in sessions[0][1]
        assert sessions[0][2] == "koan"

    def test_two_sessions_same_date(self):
        content = (
            "## 2026-02-01\n\n"
            "Session 1 (projet: koan) : A\n\n"
            "Session 2 (project: anantys-back) : B\n"
        )
        sessions = parse_summary_sessions(content)
        assert len(sessions) == 2
        assert sessions[0][2] == "koan"
        assert sessions[1][2] == "anantys-back"

    def test_sessions_across_dates(self):
        content = (
            "## 2026-01-31\n\nSession 1 : A\n\n"
            "## 2026-02-01\n\nSession 2 (projet: koan) : B\n"
        )
        sessions = parse_summary_sessions(content)
        assert len(sessions) == 2
        assert sessions[0][0] == "## 2026-01-31"
        assert sessions[1][0] == "## 2026-02-01"

    def test_empty_content(self):
        assert parse_summary_sessions("") == []

    def test_title_only(self):
        assert parse_summary_sessions("# Summary\n") == []

    def test_no_project_hint(self):
        content = "## 2026-01-31\n\nSession 1 : did stuff without tag\n"
        sessions = parse_summary_sessions(content)
        assert len(sessions) == 1
        assert sessions[0][2] == ""


# ---------------------------------------------------------------------------
# scoped_summary
# ---------------------------------------------------------------------------

class TestScopedSummary:

    def test_filters_by_project(self, tmp_path):
        mem = tmp_path / "memory"
        mem.mkdir()
        (mem / "summary.md").write_text(
            "# Summary\n\n## 2026-02-01\n\n"
            "Session 1 (projet: koan) : koan work\n\n"
            "Session 2 (project: anantys-back) : anantys work\n"
        )
        result = scoped_summary(str(tmp_path), "koan")
        assert "koan work" in result
        assert "anantys work" not in result

    def test_includes_untagged_sessions(self, tmp_path):
        mem = tmp_path / "memory"
        mem.mkdir()
        (mem / "summary.md").write_text(
            "## 2026-01-31\n\nSession 1 : old untagged work\n\n"
            "## 2026-02-01\n\nSession 2 (projet: koan) : koan work\n"
        )
        result = scoped_summary(str(tmp_path), "koan")
        assert "old untagged" in result
        assert "koan work" in result

    def test_missing_file_returns_empty(self, tmp_path):
        assert scoped_summary(str(tmp_path), "koan") == ""

    def test_preserves_title(self, tmp_path):
        mem = tmp_path / "memory"
        mem.mkdir()
        (mem / "summary.md").write_text(
            "# Résumé des sessions\n\n## 2026-02-01\n\nSession 1 (projet: koan) : work\n"
        )
        result = scoped_summary(str(tmp_path), "koan")
        assert result.startswith("# Résumé des sessions")


# ---------------------------------------------------------------------------
# compact_summary
# ---------------------------------------------------------------------------

class TestCompactSummary:

    def test_removes_old_sessions(self, tmp_path):
        mem = tmp_path / "memory"
        mem.mkdir()
        lines = ["# Summary\n"]
        for i in range(1, 16):
            lines.append(f"\n## 2026-02-{i:02d}\n\nSession {i} (projet: koan) : work {i}\n")
        (mem / "summary.md").write_text("".join(lines))

        removed = compact_summary(str(tmp_path), max_sessions=5)
        assert removed == 10
        content = (mem / "summary.md").read_text()
        assert "Session 15" in content
        assert "Session 11" in content
        assert "Session 1 " not in content

    def test_no_compaction_needed(self, tmp_path):
        mem = tmp_path / "memory"
        mem.mkdir()
        (mem / "summary.md").write_text(
            "# Summary\n\n## 2026-02-01\n\nSession 1 : work\n"
        )
        assert compact_summary(str(tmp_path), max_sessions=10) == 0

    def test_missing_file(self, tmp_path):
        assert compact_summary(str(tmp_path)) == 0

    def test_exact_count_no_removal(self, tmp_path):
        mem = tmp_path / "memory"
        mem.mkdir()
        lines = ["# Summary\n"]
        for i in range(1, 6):
            lines.append(f"\n## 2026-02-{i:02d}\n\nSession {i} : work\n")
        (mem / "summary.md").write_text("".join(lines))
        assert compact_summary(str(tmp_path), max_sessions=5) == 0


# ---------------------------------------------------------------------------
# _balanced_select
# ---------------------------------------------------------------------------

class TestBalancedSelect:
    """Tests for project-aware session selection logic."""

    @staticmethod
    def _session(date_str, project, num):
        return (f"## {date_str}", f"Session {num} (project: {project}) : work", project)

    def test_single_project_behaves_like_recency(self):
        """With only one project, balanced select = last N."""
        sessions = [self._session("2026-02-01", "koan", i) for i in range(10)]
        result = _balanced_select(sessions, max_sessions=5)
        assert len(result) == 5
        # Should keep last 5
        assert result[-1][1] == sessions[9][1]
        assert result[0][1] == sessions[5][1]

    def test_preserves_minority_project(self):
        """A project with few sessions is NOT evicted by a dominant project."""
        sessions = []
        # 2 old sessions for project B
        sessions.append(self._session("2026-01-01", "backend", 1))
        sessions.append(self._session("2026-01-02", "backend", 2))
        # 13 recent sessions for project A
        for i in range(13):
            sessions.append(self._session(f"2026-02-{i+1:02d}", "koan", 10 + i))

        result = _balanced_select(sessions, max_sessions=10)
        assert len(result) == 10
        # Backend sessions must survive
        projects = [s[2] for s in result]
        assert "backend" in projects
        assert projects.count("backend") >= 1

    def test_many_projects_budget_tight(self):
        """With many projects and tight budget, each gets at least 1 session."""
        sessions = []
        projects = ["koan", "tmf", "backend", "frontend", "traefik", "clone", "perl", "rsa"]
        for i, proj in enumerate(projects):
            sessions.append(self._session(f"2026-02-{i+1:02d}", proj, i + 1))
            sessions.append(self._session(f"2026-02-{i+10:02d}", proj, i + 100))

        # 16 sessions, 8 projects, budget=10 — each should get at least 1
        result = _balanced_select(sessions, max_sessions=10)
        assert len(result) == 10
        result_projects = set(s[2] for s in result)
        assert result_projects == set(projects)

    def test_extremely_tight_budget(self):
        """Budget < number of projects: keeps 1 per project up to budget."""
        sessions = []
        for i, proj in enumerate(["a", "b", "c", "d", "e"]):
            sessions.append(self._session(f"2026-02-{i+1:02d}", proj, i))

        # Budget of 3 for 5 projects — can't guarantee all
        result = _balanced_select(sessions, max_sessions=3)
        assert len(result) == 3
        # Should keep the 3 most recent sessions
        assert result[-1][1] == sessions[4][1]

    def test_preserves_original_order(self):
        """Selected sessions maintain chronological order."""
        sessions = [
            self._session("2026-01-01", "backend", 1),
            self._session("2026-01-15", "koan", 2),
            self._session("2026-02-01", "backend", 3),
            self._session("2026-02-15", "koan", 4),
        ]
        result = _balanced_select(sessions, max_sessions=3)
        # Verify order is preserved
        dates = [s[0] for s in result]
        assert dates == sorted(dates)

    def test_untagged_sessions_treated_as_project(self):
        """Sessions without a project hint (empty string) are grouped together."""
        sessions = [
            self._session("2026-01-01", "", 1),  # untagged
            self._session("2026-01-02", "", 2),  # untagged
            self._session("2026-02-01", "koan", 3),
            self._session("2026-02-02", "koan", 4),
            self._session("2026-02-03", "koan", 5),
            self._session("2026-02-04", "koan", 6),
            self._session("2026-02-05", "koan", 7),
        ]
        result = _balanced_select(sessions, max_sessions=5)
        assert len(result) == 5
        # At least 1 untagged session should survive
        untagged = [s for s in result if s[2] == ""]
        assert len(untagged) >= 1

    def test_two_projects_fair_split(self):
        """Two projects with equal sessions get balanced representation."""
        sessions = []
        for i in range(5):
            sessions.append(self._session(f"2026-01-{i+1:02d}", "alpha", i))
        for i in range(5):
            sessions.append(self._session(f"2026-02-{i+1:02d}", "beta", 10 + i))

        result = _balanced_select(sessions, max_sessions=6)
        assert len(result) == 6
        alpha_count = sum(1 for s in result if s[2] == "alpha")
        beta_count = sum(1 for s in result if s[2] == "beta")
        # Each project gets at least 2 (min_per_project default)
        assert alpha_count >= 2
        assert beta_count >= 2

    def test_min_per_project_customizable(self):
        """Callers can adjust the per-project minimum."""
        sessions = []
        sessions.append(self._session("2026-01-01", "rare", 1))
        for i in range(9):
            sessions.append(self._session(f"2026-02-{i+1:02d}", "common", 10 + i))

        # min_per_project=1: rare gets 1, common fills rest
        result = _balanced_select(sessions, max_sessions=5, min_per_project=1)
        rare_count = sum(1 for s in result if s[2] == "rare")
        assert rare_count == 1

    def test_no_sessions_returns_empty(self):
        assert _balanced_select([], max_sessions=5) == []

    def test_fewer_sessions_than_budget(self):
        """All sessions kept when under budget."""
        sessions = [self._session("2026-02-01", "koan", 1)]
        result = _balanced_select(sessions, max_sessions=10)
        assert len(result) == 1


class TestCompactSummaryBalanced:
    """Integration tests for project-balanced compaction through the public API."""

    def _build_summary(self, session_specs):
        """Build a summary.md from a list of (date, project, session_num) tuples."""
        lines = ["# Summary\n"]
        for date_str, project, num in session_specs:
            lines.append(f"\n## {date_str}\n\nSession {num} (project: {project}) : work on {project}\n")
        return "".join(lines)

    def test_dominant_project_doesnt_evict_others(self, tmp_path):
        """The core bug: a project burst must not wipe all other project context."""
        mem = tmp_path / "memory"
        mem.mkdir()

        specs = []
        # 3 old sessions for backend
        for i in range(3):
            specs.append((f"2026-01-{i+1:02d}", "backend", i + 1))
        # 3 old sessions for perl-versions
        for i in range(3):
            specs.append((f"2026-01-{i+10:02d}", "perl-versions", i + 10))
        # 12 recent sessions for koan (dominant)
        for i in range(12):
            specs.append((f"2026-02-{i+1:02d}", "koan", i + 100))

        (mem / "summary.md").write_text(self._build_summary(specs))

        removed = compact_summary(str(tmp_path), max_sessions=10)
        assert removed > 0

        content = (mem / "summary.md").read_text()
        # Both minority projects must have at least 1 session
        assert "backend" in content
        assert "perl-versions" in content
        # Dominant project still gets the majority
        assert content.count("project: koan") >= 5

    def test_backward_compatible_with_single_project(self, tmp_path):
        """With a single project, behaves identically to the old algorithm."""
        mem = tmp_path / "memory"
        mem.mkdir()
        lines = ["# Summary\n"]
        for i in range(1, 16):
            lines.append(f"\n## 2026-02-{i:02d}\n\nSession {i} (projet: koan) : work {i}\n")
        (mem / "summary.md").write_text("".join(lines))

        removed = compact_summary(str(tmp_path), max_sessions=5)
        assert removed == 10
        content = (mem / "summary.md").read_text()
        assert "Session 15" in content
        assert "Session 11" in content
        assert "Session 1 " not in content

    def test_many_projects_all_represented(self, tmp_path):
        """With 8 projects, each retains representation after compaction."""
        mem = tmp_path / "memory"
        mem.mkdir()

        projects = ["koan", "tmf", "backend", "frontend", "traefik", "clone", "perl", "rsa"]
        specs = []
        for idx, proj in enumerate(projects):
            for j in range(3):
                day = idx * 3 + j + 1
                specs.append((f"2026-01-{day:02d}", proj, idx * 10 + j))

        # 24 sessions, 8 projects, budget=15
        (mem / "summary.md").write_text(self._build_summary(specs))
        removed = compact_summary(str(tmp_path), max_sessions=15)
        assert removed > 0

        content = (mem / "summary.md").read_text()
        for proj in projects:
            assert proj in content, f"Project {proj} was evicted from summary"


# ---------------------------------------------------------------------------
# cleanup_learnings
# ---------------------------------------------------------------------------

class TestCleanupLearnings:

    def _write_learnings(self, tmp_path, project, content):
        p = tmp_path / "memory" / "projects" / project
        p.mkdir(parents=True, exist_ok=True)
        (p / "learnings.md").write_text(content)
        return p / "learnings.md"

    def test_removes_duplicates(self, tmp_path):
        path = self._write_learnings(tmp_path, "koan",
            "# Learnings\n\n- fact A\n- fact B\n- fact A\n- fact C\n")
        removed = cleanup_learnings(str(tmp_path), "koan")
        assert removed == 1
        content = path.read_text()
        assert content.count("fact A") == 1
        assert "fact B" in content
        assert "fact C" in content

    def test_preserves_headers_and_blanks(self, tmp_path):
        path = self._write_learnings(tmp_path, "koan",
            "# Learnings\n\n## Section A\n\n- item\n\n## Section A\n\n- item\n")
        removed = cleanup_learnings(str(tmp_path), "koan")
        assert removed == 1
        content = path.read_text()
        # Headers are preserved even if duplicated
        assert content.count("## Section A") == 2

    def test_no_duplicates(self, tmp_path):
        self._write_learnings(tmp_path, "koan",
            "# Learnings\n\n- unique A\n- unique B\n")
        assert cleanup_learnings(str(tmp_path), "koan") == 0

    def test_missing_file(self, tmp_path):
        assert cleanup_learnings(str(tmp_path), "koan") == 0

    def test_empty_file(self, tmp_path):
        self._write_learnings(tmp_path, "koan", "")
        assert cleanup_learnings(str(tmp_path), "koan") == 0


# ---------------------------------------------------------------------------
# run_cleanup
# ---------------------------------------------------------------------------

class TestRunCleanup:

    def test_runs_all_tasks(self, tmp_path):
        mem = tmp_path / "memory"
        mem.mkdir()
        # Summary with 12 sessions
        lines = ["# Summary\n"]
        for i in range(1, 13):
            lines.append(f"\n## 2026-02-{i:02d}\n\nSession {i} : work\n")
        (mem / "summary.md").write_text("".join(lines))

        # Learnings with dupes
        proj = mem / "projects" / "koan"
        proj.mkdir(parents=True)
        (proj / "learnings.md").write_text("# L\n\n- dup\n- dup\n- unique\n")

        stats = run_cleanup(str(tmp_path), max_sessions=5)
        assert stats["summary_compacted"] == 7
        assert stats["learnings_dedup_koan"] == 1

    def test_no_projects_dir(self, tmp_path):
        mem = tmp_path / "memory"
        mem.mkdir()
        (mem / "summary.md").write_text("# Summary\n\n## 2026-02-01\n\nSession 1 : work\n")
        stats = run_cleanup(str(tmp_path))
        assert stats["summary_compacted"] == 0


# ---------------------------------------------------------------------------
# _extract_session_digest
# ---------------------------------------------------------------------------

class TestExtractSessionDigest:

    def test_session_with_subheader(self):
        content = "## Session 23 — Run 1/20\n\n### Mode autonome — US 5.1\n\nLots of details...\n"
        digests = _extract_session_digest(content)
        assert len(digests) == 1
        assert "Session 23" in digests[0]
        assert "US 5.1" in digests[0]

    def test_session_without_subheader(self):
        content = "## Session 5 — Run 3/20\n\nDid stuff without sub-header.\n"
        digests = _extract_session_digest(content)
        assert len(digests) == 1
        assert "Session 5" in digests[0]

    def test_multiple_sessions(self):
        content = (
            "## Session 1 — Run 1/20\n\n### Fix bug A\n\nDetails.\n\n"
            "## Session 2 — Run 2/20\n\n### Add feature B\n\nMore details.\n"
        )
        digests = _extract_session_digest(content)
        assert len(digests) == 2
        assert "Fix bug A" in digests[0]
        assert "Add feature B" in digests[1]

    def test_empty_content(self):
        assert _extract_session_digest("") == []

    def test_no_sessions(self):
        assert _extract_session_digest("Just some text\nwithout headers\n") == []

    def test_mode_header(self):
        content = "## Mode autonome\n\n### Audit sécurité\n\nFindings...\n"
        digests = _extract_session_digest(content)
        assert len(digests) == 1
        assert "Audit sécurité" in digests[0]


# ---------------------------------------------------------------------------
# archive_journals
# ---------------------------------------------------------------------------

class TestArchiveJournals:

    def _make_journal_day(self, tmp_path, date_str, project, content):
        """Create a nested journal entry: journal/YYYY-MM-DD/project.md"""
        day_dir = tmp_path / "journal" / date_str
        day_dir.mkdir(parents=True, exist_ok=True)
        (day_dir / f"{project}.md").write_text(content)

    def test_archives_old_journals(self, tmp_path):
        old_date = (date.today() - timedelta(days=35)).strftime("%Y-%m-%d")
        old_month = old_date[:7]
        self._make_journal_day(
            tmp_path, old_date, "koan",
            "## Session 1 — Run 1/20\n\n### Fix bug\n\nDetails.\n"
        )
        stats = archive_journals(str(tmp_path), archive_after_days=30)
        assert stats["archived_days"] == 1
        assert stats["archive_lines"] == 1

        # Archive file created
        archive = tmp_path / "journal" / "archives" / old_month / "koan.md"
        assert archive.exists()
        content = archive.read_text()
        assert "Fix bug" in content
        assert old_date in content

        # Original deleted
        assert not (tmp_path / "journal" / old_date).exists()

    def test_skips_recent_journals(self, tmp_path):
        recent_date = (date.today() - timedelta(days=5)).strftime("%Y-%m-%d")
        self._make_journal_day(
            tmp_path, recent_date, "koan", "## Session 1\n\nRecent.\n"
        )
        stats = archive_journals(str(tmp_path), archive_after_days=30)
        assert stats["archived_days"] == 0
        # Original still exists
        assert (tmp_path / "journal" / recent_date).exists()

    def test_deletes_very_old_journals(self, tmp_path):
        very_old = (date.today() - timedelta(days=100)).strftime("%Y-%m-%d")
        self._make_journal_day(
            tmp_path, very_old, "koan", "## Session 1\n\n### Ancient\n\nOld.\n"
        )
        stats = archive_journals(str(tmp_path), archive_after_days=30, delete_after_days=90)
        assert stats["deleted_days"] == 1

    def test_flat_legacy_journal(self, tmp_path):
        old_date = (date.today() - timedelta(days=40)).strftime("%Y-%m-%d")
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir(parents=True)
        (journal_dir / f"{old_date}.md").write_text(
            "## Session 1\n\n### Legacy work\n\nStuff.\n"
        )
        stats = archive_journals(str(tmp_path), archive_after_days=30)
        assert stats["archived_days"] == 1
        assert not (journal_dir / f"{old_date}.md").exists()

    def test_no_journal_dir(self, tmp_path):
        stats = archive_journals(str(tmp_path))
        assert stats["archived_days"] == 0

    def test_idempotent_archive(self, tmp_path):
        """Running archive twice doesn't duplicate lines."""
        old_date = (date.today() - timedelta(days=35)).strftime("%Y-%m-%d")
        old_month = old_date[:7]
        self._make_journal_day(
            tmp_path, old_date, "koan",
            "## Session 1\n\n### Work A\n\nDetails.\n"
        )
        archive_journals(str(tmp_path), archive_after_days=30)

        # Create another day and run again
        old_date2 = (date.today() - timedelta(days=36)).strftime("%Y-%m-%d")
        self._make_journal_day(
            tmp_path, old_date2, "koan",
            "## Session 2\n\n### Work B\n\nMore.\n"
        )
        archive_journals(str(tmp_path), archive_after_days=30)

        archive = tmp_path / "journal" / "archives" / old_month / "koan.md"
        content = archive.read_text()
        # Each digest line appears exactly once
        lines = [l for l in content.splitlines() if l.strip().startswith(old_date)]
        assert len(lines) == 1

    def test_multiple_projects_same_day(self, tmp_path):
        old_date = (date.today() - timedelta(days=35)).strftime("%Y-%m-%d")
        old_month = old_date[:7]
        self._make_journal_day(
            tmp_path, old_date, "koan",
            "## Session 1\n\n### Kōan work\n\nK.\n"
        )
        self._make_journal_day(
            tmp_path, old_date, "anantys-back",
            "## Session 2\n\n### Anantys work\n\nA.\n"
        )
        stats = archive_journals(str(tmp_path), archive_after_days=30)
        assert stats["archive_lines"] == 2

        # Separate archive files per project
        assert (tmp_path / "journal" / "archives" / old_month / "koan.md").exists()
        assert (tmp_path / "journal" / "archives" / old_month / "anantys-back.md").exists()


# ---------------------------------------------------------------------------
# cap_learnings
# ---------------------------------------------------------------------------

class TestCapLearnings:

    def _write_learnings(self, tmp_path, project, content):
        p = tmp_path / "memory" / "projects" / project
        p.mkdir(parents=True, exist_ok=True)
        (p / "learnings.md").write_text(content)
        return p / "learnings.md"

    def test_caps_oversized_learnings(self, tmp_path):
        lines = ["# Learnings\n", ""]
        for i in range(300):
            lines.append(f"- fact {i}")
        path = self._write_learnings(tmp_path, "koan", "\n".join(lines))

        removed = cap_learnings(str(tmp_path), "koan", max_lines=100)
        assert removed == 200
        content = path.read_text()
        assert "fact 299" in content  # recent kept
        assert "fact 0" not in content  # old removed
        assert "archived" in content  # truncation note

    def test_no_cap_needed(self, tmp_path):
        self._write_learnings(tmp_path, "koan", "# Learnings\n\n- A\n- B\n")
        assert cap_learnings(str(tmp_path), "koan", max_lines=200) == 0

    def test_missing_file(self, tmp_path):
        assert cap_learnings(str(tmp_path), "koan") == 0

    def test_preserves_header(self, tmp_path):
        lines = ["# Learnings\n", ""]
        for i in range(50):
            lines.append(f"- fact {i}")
        path = self._write_learnings(tmp_path, "koan", "\n".join(lines))

        cap_learnings(str(tmp_path), "koan", max_lines=10)
        content = path.read_text()
        assert content.startswith("# Learnings")

    def test_marker_not_treated_as_content_on_reparse(self, tmp_path):
        """Regression: marker line must not accumulate on repeated cap runs.

        Previously the marker was f"\\n_(oldest N entries archived)_\\n" with
        embedded newlines. When re-parsed, the in_header guard treated the
        marker as a content line, causing it to accumulate on every run.
        """
        lines = ["# Learnings", ""]
        for i in range(30):
            lines.append(f"- fact {i}")
        path = self._write_learnings(tmp_path, "koan", "\n".join(lines))

        # First cap — produces marker
        removed1 = cap_learnings(str(tmp_path), "koan", max_lines=10)
        assert removed1 == 20
        content1 = path.read_text()
        assert "_(oldest 20 entries archived)_" in content1
        marker_count_1 = content1.count("_(oldest")

        # Second cap on the already-capped file — marker should NOT accumulate
        # The file now has ~10 content lines + marker + header, so it shouldn't
        # need re-capping. But if we add more lines to force it:
        lines2 = content1.rstrip("\n").split("\n")
        for i in range(15):
            lines2.append(f"- new fact {i}")
        path.write_text("\n".join(lines2) + "\n")

        removed2 = cap_learnings(str(tmp_path), "koan", max_lines=10)
        assert removed2 > 0
        content2 = path.read_text()
        # Marker should appear exactly once (the new one), not accumulate
        marker_count_2 = content2.count("_(oldest")
        assert marker_count_2 == 1, f"Marker accumulated: found {marker_count_2} markers"

    def test_marker_has_no_embedded_newlines(self, tmp_path):
        """The archive marker line should be a clean single line."""
        lines = ["# Learnings", ""]
        for i in range(25):
            lines.append(f"- fact {i}")
        path = self._write_learnings(tmp_path, "koan", "\n".join(lines))

        cap_learnings(str(tmp_path), "koan", max_lines=10)
        content_lines = path.read_text().splitlines()
        marker_lines = [l for l in content_lines if "oldest" in l and "archived" in l]
        assert len(marker_lines) == 1
        # The marker should be a clean line, not contain embedded \n
        assert marker_lines[0].strip() == f"_(oldest 15 entries archived)_"


# ---------------------------------------------------------------------------
# Archive safety: write archives BEFORE deleting sources
# ---------------------------------------------------------------------------

class TestArchiveSafety:
    """Tests verifying the archive-before-delete ordering."""

    def _make_journal_day(self, tmp_path, date_str, project, content):
        day_dir = tmp_path / "journal" / date_str
        day_dir.mkdir(parents=True, exist_ok=True)
        (day_dir / f"{project}.md").write_text(content)

    def test_archive_written_before_source_deleted(self, tmp_path):
        """Verify archive file exists even if deletion would fail."""
        old_date = (date.today() - timedelta(days=35)).strftime("%Y-%m-%d")
        old_month = old_date[:7]
        self._make_journal_day(
            tmp_path, old_date, "koan",
            "## Session 1\n\n### Important work\n\nDetails.\n"
        )

        stats = archive_journals(str(tmp_path), archive_after_days=30)
        archive = tmp_path / "journal" / "archives" / old_month / "koan.md"
        assert archive.exists()
        assert "Important work" in archive.read_text()
        assert stats["archived_days"] == 1

    def test_archive_survives_rmtree_failure(self, tmp_path):
        """If rmtree fails, archive is still written and stats reflect partial success."""
        old_date = (date.today() - timedelta(days=35)).strftime("%Y-%m-%d")
        old_month = old_date[:7]
        self._make_journal_day(
            tmp_path, old_date, "koan",
            "## Session 1\n\n### Critical data\n\nMust survive.\n"
        )

        original_rmtree = __import__("shutil").rmtree

        def failing_rmtree(path, **kwargs):
            raise OSError("Permission denied")

        with patch("app.memory_manager.shutil.rmtree", side_effect=failing_rmtree):
            stats = archive_journals(str(tmp_path), archive_after_days=30)

        # Archive was written despite deletion failure
        archive = tmp_path / "journal" / "archives" / old_month / "koan.md"
        assert archive.exists()
        assert "Critical data" in archive.read_text()
        # Source still exists (deletion failed)
        assert (tmp_path / "journal" / old_date).exists()
        # No days counted as archived/deleted since deletion failed
        assert stats["archived_days"] == 0
        assert stats["deleted_days"] == 0

    def test_archive_survives_unlink_failure_legacy(self, tmp_path):
        """Legacy flat journal: archive written even if unlink fails."""
        old_date = (date.today() - timedelta(days=40)).strftime("%Y-%m-%d")
        old_month = old_date[:7]
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir(parents=True)
        (journal_dir / f"{old_date}.md").write_text(
            "## Session 1\n\n### Legacy data\n\nOld stuff.\n"
        )

        def failing_unlink(missing_ok=False):
            raise OSError("Read-only filesystem")

        with patch.object(type(journal_dir / f"{old_date}.md"), "unlink", failing_unlink):
            stats = archive_journals(str(tmp_path), archive_after_days=30)

        archive = tmp_path / "journal" / "archives" / old_month / "legacy.md"
        assert archive.exists()
        assert "Legacy data" in archive.read_text()

    def test_multiple_days_partial_delete_failure(self, tmp_path):
        """If one day fails to delete, others still succeed."""
        dates = []
        for offset in [35, 36, 37]:
            d = (date.today() - timedelta(days=offset)).strftime("%Y-%m-%d")
            dates.append(d)
            self._make_journal_day(
                tmp_path, d, "koan",
                f"## Session {offset}\n\n### Work {offset}\n\nDetails.\n"
            )

        call_count = [0]
        original_rmtree = __import__("shutil").rmtree

        def sometimes_failing_rmtree(path, **kwargs):
            call_count[0] += 1
            if call_count[0] == 2:
                raise OSError("Transient failure")
            original_rmtree(path, **kwargs)

        with patch("app.memory_manager.shutil.rmtree", side_effect=sometimes_failing_rmtree):
            stats = archive_journals(str(tmp_path), archive_after_days=30)

        # 2 of 3 days deleted successfully
        assert stats["archived_days"] == 2


# ---------------------------------------------------------------------------
# File I/O error handling
# ---------------------------------------------------------------------------

class TestFileErrorHandling:

    def _write_learnings(self, tmp_path, project, content):
        p = tmp_path / "memory" / "projects" / project
        p.mkdir(parents=True, exist_ok=True)
        (p / "learnings.md").write_text(content)
        return p / "learnings.md"

    def test_cleanup_learnings_unreadable_file(self, tmp_path):
        """cleanup_learnings returns 0 on read error, doesn't crash."""
        path = self._write_learnings(tmp_path, "koan", "# Learnings\n\n- dup\n- dup\n")
        with patch.object(type(path), "read_text", side_effect=OSError("Permission denied")):
            result = cleanup_learnings(str(tmp_path), "koan")
        assert result == 0

    def test_cap_learnings_unreadable_file(self, tmp_path):
        """cap_learnings returns 0 on read error, doesn't crash."""
        lines = ["# L\n", ""]
        for i in range(300):
            lines.append(f"- fact {i}")
        path = self._write_learnings(tmp_path, "koan", "\n".join(lines))
        with patch.object(type(path), "read_text", side_effect=UnicodeDecodeError("utf-8", b"", 0, 1, "bad")):
            result = cap_learnings(str(tmp_path), "koan", max_lines=10)
        assert result == 0

    def test_archive_skips_unreadable_journal_file(self, tmp_path):
        """Unreadable journal file is skipped, others still processed."""
        old_date1 = (date.today() - timedelta(days=35)).strftime("%Y-%m-%d")
        old_date2 = (date.today() - timedelta(days=36)).strftime("%Y-%m-%d")
        old_month = old_date1[:7]

        for d in [old_date1, old_date2]:
            day_dir = tmp_path / "journal" / d
            day_dir.mkdir(parents=True, exist_ok=True)
            (day_dir / "koan.md").write_text(
                f"## Session\n\n### Work {d}\n\nDetails.\n"
            )

        original_read_text = type(tmp_path / "journal" / old_date1 / "koan.md").read_text
        calls = [0]

        def selective_read_error(self_path, *args, **kwargs):
            calls[0] += 1
            if old_date1 in str(self_path) and "koan.md" in str(self_path):
                raise OSError("Disk error")
            return original_read_text(self_path, *args, **kwargs)

        with patch("pathlib.PosixPath.read_text", selective_read_error):
            stats = archive_journals(str(tmp_path), archive_after_days=30)

        # At least one day was processed
        assert stats["archive_lines"] >= 1

    def test_run_cleanup_projects_dir_is_file(self, tmp_path):
        """run_cleanup handles projects_dir being a file (not directory)."""
        mem = tmp_path / "memory"
        mem.mkdir()
        (mem / "summary.md").write_text("# Summary\n")
        (mem / "projects").write_text("oops")  # file, not dir

        stats = run_cleanup(str(tmp_path))
        assert stats["summary_compacted"] == 0


# ---------------------------------------------------------------------------
# Archive atomic write safety
# ---------------------------------------------------------------------------

class TestArchiveAtomicWrite:

    def _make_journal_day(self, tmp_path, date_str, project, content):
        day_dir = tmp_path / "journal" / date_str
        day_dir.mkdir(parents=True, exist_ok=True)
        (day_dir / f"{project}.md").write_text(content)

    def test_archive_creates_valid_file(self, tmp_path):
        """Verify archive writes produce valid, complete files."""
        old_date = (date.today() - timedelta(days=35)).strftime("%Y-%m-%d")
        old_month = old_date[:7]
        self._make_journal_day(
            tmp_path, old_date, "koan",
            "## Session 1\n\n### Work\n\nDetails.\n"
        )

        archive_journals(str(tmp_path), archive_after_days=30)

        archive = tmp_path / "journal" / "archives" / old_month / "koan.md"
        assert archive.exists()
        content = archive.read_text()
        assert content.startswith("# Journal archive")
        assert "Work" in content
        # File should be complete (no partial writes)
        assert content.endswith("\n")

    def test_archive_append_preserves_existing_content(self, tmp_path):
        """When appending to an existing archive, existing content is preserved."""
        # Use mid-month dates to guarantee both land in the same month
        ref = date.today() - timedelta(days=45)
        d1 = ref.replace(day=15)
        d2 = ref.replace(day=14)
        old_date1 = d1.strftime("%Y-%m-%d")
        old_date2 = d2.strftime("%Y-%m-%d")
        old_month = old_date1[:7]

        # Create first journal day
        self._make_journal_day(
            tmp_path, old_date1, "koan",
            "## Session 1\n\n### First work\n\nDetails.\n"
        )

        # Archive it
        archive_journals(str(tmp_path), archive_after_days=30)

        archive = tmp_path / "journal" / "archives" / old_month / "koan.md"
        assert archive.exists()
        first_content = archive.read_text()
        assert "First work" in first_content

        # Create second journal day
        self._make_journal_day(
            tmp_path, old_date2, "koan",
            "## Session 2\n\n### Second work\n\nMore details.\n"
        )

        # Archive again — should append
        archive_journals(str(tmp_path), archive_after_days=30)

        final_content = archive.read_text()
        assert "First work" in final_content  # original preserved
        assert "Second work" in final_content  # new content appended


# ---------------------------------------------------------------------------
# MemoryManager class tests
# ---------------------------------------------------------------------------

class TestMemoryManagerClass:

    def test_constructor_sets_paths(self, tmp_path):
        mgr = MemoryManager(str(tmp_path))
        assert mgr.memory_dir == tmp_path / "memory"
        assert mgr.journal_dir == tmp_path / "journal"
        assert mgr.summary_path == tmp_path / "memory" / "summary.md"
        assert mgr.projects_dir == tmp_path / "memory" / "projects"

    def test_learnings_path(self, tmp_path):
        mgr = MemoryManager(str(tmp_path))
        assert mgr._learnings_path("koan") == tmp_path / "memory" / "projects" / "koan" / "learnings.md"

    def test_run_cleanup_caps_learnings(self, tmp_path):
        """run_cleanup calls cap_learnings and respects max_learnings_lines."""
        proj = tmp_path / "memory" / "projects" / "koan"
        proj.mkdir(parents=True)
        mem = tmp_path / "memory"
        (mem / "summary.md").write_text("# Summary\n")

        lines = ["# Learnings\n", ""]
        for i in range(300):
            lines.append(f"- fact {i}")
        (proj / "learnings.md").write_text("\n".join(lines))

        stats = run_cleanup(str(tmp_path), max_learnings_lines=50)
        assert stats.get("learnings_capped_koan", 0) == 250
        content = (proj / "learnings.md").read_text()
        assert "fact 299" in content
        assert "fact 0" not in content


# ---------------------------------------------------------------------------
# CLI __main__ interface
# ---------------------------------------------------------------------------

class TestCLIMainBlock:
    """Test the CLI interface via runpy."""

    def test_no_args_exits_1(self, capsys):
        """CLI with no arguments prints usage and exits 1."""
        import sys
        from unittest.mock import patch
        from tests._helpers import run_module

        with patch.object(sys, "argv", ["memory_manager", ]):
            with pytest.raises(SystemExit) as exc_info:
                run_module("app.memory_manager", run_name="__main__")
            assert exc_info.value.code == 1

    def test_only_instance_dir_exits_1(self, tmp_path, capsys):
        """CLI with only instance_dir (no command) exits 1."""
        import sys
        from unittest.mock import patch
        from tests._helpers import run_module

        with patch.object(sys, "argv", ["memory_manager", str(tmp_path)]):
            with pytest.raises(SystemExit) as exc_info:
                run_module("app.memory_manager", run_name="__main__")
            assert exc_info.value.code == 1

    def test_unknown_command_exits_1(self, tmp_path, capsys):
        """CLI with unknown command exits 1."""
        import sys
        from unittest.mock import patch
        from tests._helpers import run_module
        from io import StringIO

        err = StringIO()
        with patch.object(sys, "argv", ["memory_manager", str(tmp_path), "bogus"]):
            with patch("sys.stderr", err):
                with pytest.raises(SystemExit) as exc_info:
                    run_module("app.memory_manager", run_name="__main__")
            assert exc_info.value.code == 1
        assert "Unknown command: bogus" in err.getvalue()

    def test_scoped_summary_command(self, tmp_path):
        """CLI scoped-summary outputs filtered summary."""
        import sys
        from unittest.mock import patch
        from io import StringIO
        from tests._helpers import run_module

        mem = tmp_path / "memory"
        mem.mkdir()
        (mem / "summary.md").write_text(
            "# Summary\n\n## 2026-02-01\n\n"
            "Session 1 (project: koan) : koan work\n\n"
            "Session 2 (project: other) : other work\n"
        )

        out = StringIO()
        with patch.object(sys, "argv", ["memory_manager", str(tmp_path), "scoped-summary", "koan"]):
            with patch("sys.stdout", out):
                try:
                    run_module("app.memory_manager", run_name="__main__")
                except SystemExit:
                    pass
        assert "koan work" in out.getvalue()
        assert "other work" not in out.getvalue()

    def test_scoped_summary_no_project_exits_1(self, tmp_path):
        """CLI scoped-summary without project name exits 1."""
        import sys
        from unittest.mock import patch
        from tests._helpers import run_module

        with patch.object(sys, "argv", ["memory_manager", str(tmp_path), "scoped-summary"]):
            with pytest.raises(SystemExit) as exc_info:
                run_module("app.memory_manager", run_name="__main__")
            assert exc_info.value.code == 1

    def test_compact_command(self, tmp_path):
        """CLI compact reports removal count."""
        import sys
        from unittest.mock import patch
        from io import StringIO
        from tests._helpers import run_module

        mem = tmp_path / "memory"
        mem.mkdir()
        lines = ["# Summary\n"]
        for i in range(1, 12):
            lines.append(f"\n## 2026-02-{i:02d}\n\nSession {i} : work\n")
        (mem / "summary.md").write_text("".join(lines))

        out = StringIO()
        with patch.object(sys, "argv", ["memory_manager", str(tmp_path), "compact", "5"]):
            with patch("sys.stdout", out):
                try:
                    run_module("app.memory_manager", run_name="__main__")
                except SystemExit:
                    pass
        assert "Compacted: 6 sessions removed" in out.getvalue()

    def test_compact_default_max(self, tmp_path):
        """CLI compact without max_sessions defaults to 15."""
        import sys
        from unittest.mock import patch
        from io import StringIO
        from tests._helpers import run_module

        mem = tmp_path / "memory"
        mem.mkdir()
        (mem / "summary.md").write_text("# Summary\n\n## 2026-02-01\n\nSession 1 : work\n")

        out = StringIO()
        with patch.object(sys, "argv", ["memory_manager", str(tmp_path), "compact"]):
            with patch("sys.stdout", out):
                try:
                    run_module("app.memory_manager", run_name="__main__")
                except SystemExit:
                    pass
        assert "Compacted: 0 sessions removed" in out.getvalue()

    def test_cleanup_learnings_command(self, tmp_path):
        """CLI cleanup-learnings reports dedup count."""
        import sys
        from unittest.mock import patch
        from io import StringIO
        from tests._helpers import run_module

        proj = tmp_path / "memory" / "projects" / "koan"
        proj.mkdir(parents=True)
        (proj / "learnings.md").write_text("# L\n\n- dup\n- dup\n- unique\n")

        out = StringIO()
        with patch.object(sys, "argv", ["memory_manager", str(tmp_path), "cleanup-learnings", "koan"]):
            with patch("sys.stdout", out):
                try:
                    run_module("app.memory_manager", run_name="__main__")
                except SystemExit:
                    pass
        assert "Deduped: 1 lines removed" in out.getvalue()

    def test_cleanup_learnings_no_project_exits_1(self, tmp_path):
        """CLI cleanup-learnings without project name exits 1."""
        import sys
        from unittest.mock import patch
        from tests._helpers import run_module

        with patch.object(sys, "argv", ["memory_manager", str(tmp_path), "cleanup-learnings"]):
            with pytest.raises(SystemExit) as exc_info:
                run_module("app.memory_manager", run_name="__main__")
            assert exc_info.value.code == 1

    def test_archive_journals_command(self, tmp_path):
        """CLI archive-journals reports stats."""
        import sys
        from unittest.mock import patch
        from io import StringIO
        from tests._helpers import run_module
        from datetime import date, timedelta

        old_date = (date.today() - timedelta(days=35)).strftime("%Y-%m-%d")
        day_dir = tmp_path / "journal" / old_date
        day_dir.mkdir(parents=True)
        (day_dir / "koan.md").write_text("## Session 1\n\n### Work\n\nDetails.\n")

        out = StringIO()
        with patch.object(sys, "argv", ["memory_manager", str(tmp_path), "archive-journals"]):
            with patch("sys.stdout", out):
                try:
                    run_module("app.memory_manager", run_name="__main__")
                except SystemExit:
                    pass
        output = out.getvalue()
        assert "archived_days" in output

    def test_archive_journals_custom_days(self, tmp_path):
        """CLI archive-journals with custom days argument."""
        import sys
        from unittest.mock import patch
        from io import StringIO
        from tests._helpers import run_module

        (tmp_path / "journal").mkdir(parents=True)

        out = StringIO()
        with patch.object(sys, "argv", ["memory_manager", str(tmp_path), "archive-journals", "7"]):
            with patch("sys.stdout", out):
                try:
                    run_module("app.memory_manager", run_name="__main__")
                except SystemExit:
                    pass
        output = out.getvalue()
        assert "archived_days" in output

    def test_cleanup_command(self, tmp_path):
        """CLI cleanup runs all tasks and reports stats."""
        import sys
        from unittest.mock import patch
        from io import StringIO
        from tests._helpers import run_module

        mem = tmp_path / "memory"
        mem.mkdir()
        (mem / "summary.md").write_text("# Summary\n\n## 2026-02-01\n\nSession 1 : work\n")

        out = StringIO()
        with patch.object(sys, "argv", ["memory_manager", str(tmp_path), "cleanup"]):
            with patch("sys.stdout", out):
                try:
                    run_module("app.memory_manager", run_name="__main__")
                except SystemExit:
                    pass
        output = out.getvalue()
        assert "summary_compacted" in output

    def test_cleanup_custom_max_sessions(self, tmp_path):
        """CLI cleanup with custom max_sessions argument."""
        import sys
        from unittest.mock import patch
        from io import StringIO
        from tests._helpers import run_module

        mem = tmp_path / "memory"
        mem.mkdir()
        lines = ["# Summary\n"]
        for i in range(1, 25):
            lines.append(f"\n## 2026-02-{i:02d}\n\nSession {i} : work\n")
        (mem / "summary.md").write_text("".join(lines))

        out = StringIO()
        with patch.object(sys, "argv", ["memory_manager", str(tmp_path), "cleanup", "5"]):
            with patch("sys.stdout", out):
                try:
                    run_module("app.memory_manager", run_name="__main__")
                except SystemExit:
                    pass
        output = out.getvalue()
        assert "summary_compacted" in output
