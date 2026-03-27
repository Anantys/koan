"""Tests for daily_report.py — report generation, mission parsing, time logic."""

from tests._helpers import run_module
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from app.daily_report import (
    should_send_report,
    _read_journal,
    _extract_mission_title,
    _parse_completed_missions,
    _count_pending_missions,
    generate_report,
    send_daily_report,
    mark_report_sent,
)


# ---------------------------------------------------------------------------
# should_send_report
# ---------------------------------------------------------------------------

class TestShouldSendReport:
    def test_morning_window(self, tmp_path):
        morning = datetime(2026, 2, 1, 8, 0)
        with patch("app.daily_report.datetime") as mock_dt, \
             patch("app.daily_report.REPORT_MARKER", tmp_path / ".marker"):
            mock_dt.now.return_value = morning
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            result = should_send_report()
        assert result == "morning"

    def test_outside_window(self, tmp_path):
        noon = datetime(2026, 2, 1, 12, 0)
        with patch("app.daily_report.datetime") as mock_dt, \
             patch("app.daily_report.REPORT_MARKER", tmp_path / ".marker"):
            mock_dt.now.return_value = noon
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            result = should_send_report()
        assert result is None

    def test_evening_with_quota(self, tmp_path):
        evening = datetime(2026, 2, 1, 21, 0)
        quota_file = tmp_path / ".koan-quota-reset"
        quota_file.write_text("resets 7am")
        with patch("app.daily_report.datetime") as mock_dt, \
             patch("app.daily_report.REPORT_MARKER", tmp_path / ".marker"), \
             patch("app.daily_report.KOAN_ROOT", tmp_path):
            mock_dt.now.return_value = evening
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            result = should_send_report()
        assert result == "evening"

    def test_no_duplicate_report(self, tmp_path):
        morning = datetime(2026, 2, 1, 8, 0)
        marker = tmp_path / ".marker"
        marker.write_text("2026-02-01")
        with patch("app.daily_report.datetime") as mock_dt, \
             patch("app.daily_report.REPORT_MARKER", marker):
            mock_dt.now.return_value = morning
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            result = should_send_report()
        assert result is None


# ---------------------------------------------------------------------------
# _read_journal
# ---------------------------------------------------------------------------

class TestReadJournal:
    def test_nested_journal(self, tmp_path):
        with patch("app.daily_report.INSTANCE_DIR", tmp_path):
            journal_dir = tmp_path / "journal" / "2026-02-01"
            journal_dir.mkdir(parents=True)
            (journal_dir / "koan.md").write_text("## Session 28\nDid stuff.")
            result = _read_journal(date(2026, 2, 1))
        assert "Session 28" in result
        assert "[koan]" in result

    def test_missing_journal(self, tmp_path):
        with patch("app.daily_report.INSTANCE_DIR", tmp_path):
            result = _read_journal(date(2026, 2, 1))
        assert result == ""


# ---------------------------------------------------------------------------
# _extract_mission_title
# ---------------------------------------------------------------------------

class TestExtractMissionTitle:
    def test_standard_format_with_project_tag(self):
        line = "- [project:koan] fix the auth bug ⏳(2026-02-17T16:00) ▶(2026-02-17T16:12) ✅ (2026-02-17 21:16)"
        assert _extract_mission_title(line) == "fix the auth bug"

    def test_standard_format_without_timestamps(self):
        assert _extract_mission_title("- [project:koan] fix the bug") == "fix the bug"

    def test_no_project_tag(self):
        assert _extract_mission_title("- fix the bug ✅ (2026-02-17 21:16)") == "fix the bug"

    def test_legacy_bold_format(self):
        assert _extract_mission_title("- **Fix IDOR** (session 22)") == "Fix IDOR"

    def test_failed_marker(self):
        line = "- [project:koan] broken thing ⏳(2026-02-16T11:38) ▶(2026-02-16T11:38) ❌ (2026-02-16 11:38)"
        assert _extract_mission_title(line) == "broken thing"

    def test_not_a_mission_line(self):
        assert _extract_mission_title("some random text") is None
        assert _extract_mission_title("## Section header") is None

    def test_empty_after_strip(self):
        assert _extract_mission_title("- ") is None

    def test_dash_separator_metadata(self):
        line = "- [project:koan] do stuff — PR #271"
        assert _extract_mission_title(line) == "do stuff"

    def test_queued_and_started_only(self):
        line = "- [project:koan] working on it ⏳(2026-02-18T08:00) ▶(2026-02-18T08:05)"
        assert _extract_mission_title(line) == "working on it"

    def test_plain_mission(self):
        assert _extract_mission_title("- simple task") == "simple task"


# ---------------------------------------------------------------------------
# _parse_completed_missions
# ---------------------------------------------------------------------------

class TestParseCompletedMissions:
    def test_real_format_with_timestamps(self, tmp_path):
        missions_file = tmp_path / "missions.md"
        missions_file.write_text(
            "# Missions\n\n"
            "## Done\n\n"
            "- [project:koan] fix auth bug ⏳(2026-02-17T16:00) ▶(2026-02-17T16:12) ✅ (2026-02-17 21:16)\n"
            "- [project:wp-toolkit] plan for case EXTWPTOOLK-11339 ✅ (2026-02-17 16:12)\n"
        )
        with patch("app.daily_report.MISSIONS_FILE", missions_file):
            result = _parse_completed_missions()
        assert len(result) == 2
        assert "fix auth bug" in result[0]
        assert "plan for case EXTWPTOOLK-11339" in result[1]

    def test_legacy_bold_entries(self, tmp_path):
        missions_file = tmp_path / "missions.md"
        missions_file.write_text(
            "# Missions\n\n"
            "## Done\n\n"
            "- **Fix IDOR** (session 22)\n"
            "- **Dunning emails** — session 20\n"
        )
        with patch("app.daily_report.MISSIONS_FILE", missions_file):
            result = _parse_completed_missions()
        assert len(result) == 2
        assert "Fix IDOR" in result[0]
        assert "Dunning emails" in result[1]

    def test_mixed_formats(self, tmp_path):
        missions_file = tmp_path / "missions.md"
        missions_file.write_text(
            "# Missions\n\n"
            "## Done\n\n"
            "- [project:koan] new format ✅ (2026-02-17 21:16)\n"
            "- **Old format** (session 1)\n"
            "- plain entry no tags\n"
        )
        with patch("app.daily_report.MISSIONS_FILE", missions_file):
            result = _parse_completed_missions()
        assert len(result) == 3

    def test_empty_done_section(self, tmp_path):
        missions_file = tmp_path / "missions.md"
        missions_file.write_text("# Missions\n\n## Done\n\n")
        with patch("app.daily_report.MISSIONS_FILE", missions_file):
            result = _parse_completed_missions()
        assert result == []

    def test_date_filter_matches(self, tmp_path):
        missions_file = tmp_path / "missions.md"
        missions_file.write_text(
            "# Missions\n\n"
            "## Done\n\n"
            "- [project:koan] yesterdays task ✅ (2026-02-17 21:16)\n"
            "- [project:koan] todays task ✅ (2026-02-18 10:30)\n"
        )
        with patch("app.daily_report.MISSIONS_FILE", missions_file):
            result = _parse_completed_missions(target_date=date(2026, 2, 18))
        assert len(result) == 1
        assert "todays task" in result[0]

    def test_date_filter_no_match(self, tmp_path):
        missions_file = tmp_path / "missions.md"
        missions_file.write_text(
            "# Missions\n\n"
            "## Done\n\n"
            "- [project:koan] old task ✅ (2026-02-15 10:00)\n"
        )
        with patch("app.daily_report.MISSIONS_FILE", missions_file):
            result = _parse_completed_missions(target_date=date(2026, 2, 18))
        assert result == []

    def test_no_date_filter_returns_all(self, tmp_path):
        """Without target_date, all done missions are returned."""
        missions_file = tmp_path / "missions.md"
        missions_file.write_text(
            "# Missions\n\n"
            "## Done\n\n"
            "- [project:koan] task A ✅ (2026-02-15 10:00)\n"
            "- [project:koan] task B ✅ (2026-02-18 10:00)\n"
        )
        with patch("app.daily_report.MISSIONS_FILE", missions_file):
            result = _parse_completed_missions()
        assert len(result) == 2

    def test_date_filter_skips_missions_without_timestamp(self, tmp_path):
        """Missions without ✅ timestamp are skipped when date filter is active."""
        missions_file = tmp_path / "missions.md"
        missions_file.write_text(
            "# Missions\n\n"
            "## Done\n\n"
            "- [project:koan] has timestamp ✅ (2026-02-18 10:00)\n"
            "- plain entry no timestamp\n"
        )
        with patch("app.daily_report.MISSIONS_FILE", missions_file):
            result = _parse_completed_missions(target_date=date(2026, 2, 18))
        assert len(result) == 1
        assert "has timestamp" in result[0]


# ---------------------------------------------------------------------------
# _count_pending_missions
# ---------------------------------------------------------------------------

class TestCountPendingMissions:
    def test_count(self, tmp_path):
        missions_file = tmp_path / "missions.md"
        missions_file.write_text(
            "# Missions\n\n"
            "## Pending\n\n"
            "- task 1\n"
            "- task 2\n\n"
            "## In Progress\n\n"
        )
        with patch("app.daily_report.MISSIONS_FILE", missions_file):
            assert _count_pending_missions() == 2

    def test_no_pending(self, tmp_path):
        missions_file = tmp_path / "missions.md"
        missions_file.write_text("# Missions\n\n## Pending\n\n(none)\n\n## In Progress\n\n")
        with patch("app.daily_report.MISSIONS_FILE", missions_file):
            assert _count_pending_missions() == 0


# ---------------------------------------------------------------------------
# generate_report
# ---------------------------------------------------------------------------

class TestGenerateReport:
    def test_morning_report_real_format(self, tmp_path):
        yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
        today = date.today().strftime("%Y-%m-%d")
        missions_file = tmp_path / "missions.md"
        missions_file.write_text(
            "# Missions\n\n"
            "## Pending\n\n"
            "- [project:koan] task 1\n\n"
            "## In Progress\n\n"
            "- [project:koan] working on stuff ⏳(2026-02-18T08:00) ▶(2026-02-18T08:05)\n\n"
            "## Done\n\n"
            f"- [project:koan] done thing ✅ ({yesterday} 21:16)\n"
            f"- [project:koan] older thing ✅ (2025-01-01 10:00)\n"
        )
        with patch("app.daily_report.MISSIONS_FILE", missions_file), \
             patch("app.daily_report.INSTANCE_DIR", tmp_path):
            report = generate_report("morning")

        assert "Report for" in report
        assert "done thing" in report
        # Older mission should be filtered out (wrong date)
        assert "older thing" not in report
        assert "Pending: 1" in report
        assert "working on stuff" in report
        assert "In Progress:" in report
        assert "-- Kōan" in report

    def test_morning_report_legacy_format(self, tmp_path):
        yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
        missions_file = tmp_path / "missions.md"
        missions_file.write_text(
            "# Missions\n\n"
            "## Pending\n\n"
            "- task 1\n\n"
            "## In Progress\n\n"
            "### Big project (PRIO)\n"
            "- sub-item\n\n"
            "## Done\n\n"
            f"- **Done thing** ✅ ({yesterday} 15:00)\n"
        )
        with patch("app.daily_report.MISSIONS_FILE", missions_file), \
             patch("app.daily_report.INSTANCE_DIR", tmp_path):
            report = generate_report("morning")

        assert "Report for" in report
        assert "Done thing" in report
        assert "Pending: 1" in report
        assert "Big project" in report
        assert "-- Kōan" in report

    def test_evening_report(self, tmp_path):
        missions_file = tmp_path / "missions.md"
        missions_file.write_text("# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n\n")
        with patch("app.daily_report.MISSIONS_FILE", missions_file), \
             patch("app.daily_report.INSTANCE_DIR", tmp_path):
            report = generate_report("evening")

        assert "Daily Summary" in report
        assert "-- Kōan" in report

    def test_journal_activities_extracted(self, tmp_path):
        """Journal ## headers should appear as activities in the report."""
        missions_file = tmp_path / "missions.md"
        missions_file.write_text("# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n\n")
        journal_dir = tmp_path / "journal" / date.today().strftime("%Y-%m-%d")
        journal_dir.mkdir(parents=True)
        (journal_dir / "koan.md").write_text(
            "## Session 75 — Run 7/20\n\n"
            "### Mode autonome — Coverage boost\n\nDid stuff.\n"
        )
        with patch("app.daily_report.MISSIONS_FILE", missions_file), \
             patch("app.daily_report.INSTANCE_DIR", tmp_path):
            report = generate_report("evening")
        assert "Activity:" in report
        assert "Session 75" in report

    def test_journal_timestamps_stripped(self, tmp_path):
        """Timestamps like '-- 15:30' should be stripped from activity lines."""
        missions_file = tmp_path / "missions.md"
        missions_file.write_text("# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n\n")
        journal_dir = tmp_path / "journal" / date.today().strftime("%Y-%m-%d")
        journal_dir.mkdir(parents=True)
        (journal_dir / "koan.md").write_text("## Git Sync — 15:30\n\nStuff.\n")
        with patch("app.daily_report.MISSIONS_FILE", missions_file), \
             patch("app.daily_report.INSTANCE_DIR", tmp_path):
            report = generate_report("evening")
        # The "— 15:30" part should be stripped
        assert "15:30" not in report
        assert "Git Sync" in report

    def test_no_missions_file(self, tmp_path):
        """Report should work even if missions.md doesn't exist."""
        missing_file = tmp_path / "nonexistent.md"
        with patch("app.daily_report.MISSIONS_FILE", missing_file), \
             patch("app.daily_report.INSTANCE_DIR", tmp_path):
            report = generate_report("morning")
        assert "Report for" in report
        assert "-- Kōan" in report

    def test_in_progress_with_real_missions(self, tmp_path):
        """In-progress missions in real format should appear in report."""
        missions_file = tmp_path / "missions.md"
        missions_file.write_text(
            "# Missions\n\n"
            "## Pending\n\n"
            "## In Progress\n\n"
            "- [project:koan] refactoring utils ⏳(2026-02-18T08:00) ▶(2026-02-18T08:05)\n"
            "- [project:ulc] fixing SSL cert reuse\n\n"
            "## Done\n\n"
        )
        with patch("app.daily_report.MISSIONS_FILE", missions_file), \
             patch("app.daily_report.INSTANCE_DIR", tmp_path):
            report = generate_report("evening")
        assert "In Progress:" in report
        assert "refactoring utils" in report
        assert "fixing SSL cert reuse" in report


# ---------------------------------------------------------------------------
# mark_report_sent
# ---------------------------------------------------------------------------

class TestMarkReportSent:
    def test_creates_marker(self, tmp_path):
        marker = tmp_path / ".marker"
        with patch("app.daily_report.REPORT_MARKER", marker):
            mark_report_sent()
        assert marker.exists()
        assert marker.read_text() == date.today().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# send_daily_report
# ---------------------------------------------------------------------------

class TestSendDailyReport:
    def test_send_success(self, tmp_path):
        missions_file = tmp_path / "missions.md"
        missions_file.write_text("# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n\n")
        marker = tmp_path / ".marker"
        with patch("app.daily_report.format_and_send", return_value=True) as mock_send, \
             patch("app.daily_report.MISSIONS_FILE", missions_file), \
             patch("app.daily_report.INSTANCE_DIR", tmp_path), \
             patch("app.daily_report.REPORT_MARKER", marker):
            result = send_daily_report("morning")
        assert result is True
        mock_send.assert_called_once()
        assert marker.exists()

    def test_send_failure(self, tmp_path):
        missions_file = tmp_path / "missions.md"
        missions_file.write_text("# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n\n")
        marker = tmp_path / ".marker"
        with patch("app.daily_report.format_and_send", return_value=False), \
             patch("app.daily_report.MISSIONS_FILE", missions_file), \
             patch("app.daily_report.INSTANCE_DIR", tmp_path), \
             patch("app.daily_report.REPORT_MARKER", marker):
            result = send_daily_report("morning")
        assert result is False
        assert not marker.exists()

    def test_auto_detect_none(self, tmp_path):
        """When no report_type given and should_send_report returns None, don't send."""
        with patch("app.daily_report.should_send_report", return_value=None):
            result = send_daily_report()
        assert result is False

    def test_auto_detect_morning(self, tmp_path):
        """Auto-detect morning report."""
        missions_file = tmp_path / "missions.md"
        missions_file.write_text("# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n\n")
        marker = tmp_path / ".marker"
        with patch("app.daily_report.should_send_report", return_value="morning"), \
             patch("app.daily_report.format_and_send", return_value=True), \
             patch("app.daily_report.MISSIONS_FILE", missions_file), \
             patch("app.daily_report.INSTANCE_DIR", tmp_path), \
             patch("app.daily_report.REPORT_MARKER", marker):
            result = send_daily_report()
        assert result is True


# ---------------------------------------------------------------------------
# CLI __main__
# ---------------------------------------------------------------------------

class TestDailyReportCLI:
    def test_cli_morning_flag(self, tmp_path, monkeypatch):
        # Setup KOAN_ROOT before module reload
        instance = tmp_path / "instance"
        instance.mkdir()
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))

        missions_file = instance / "missions.md"
        missions_file.write_text("# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n\n")
        with patch("sys.argv", ["daily_report.py", "--morning"]), \
             patch("app.notify.format_and_send", return_value=True), \
             patch("app.daily_report.format_and_send", return_value=True), \
             pytest.raises(SystemExit) as exc_info:
            run_module("app.daily_report", run_name="__main__")
        assert exc_info.value.code == 0

    def test_cli_evening_flag(self, tmp_path, monkeypatch):
        # Setup KOAN_ROOT before module reload
        instance = tmp_path / "instance"
        instance.mkdir()
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))

        missions_file = instance / "missions.md"
        missions_file.write_text("# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n\n")
        with patch("sys.argv", ["daily_report.py", "--evening"]), \
             patch("app.notify.format_and_send", return_value=True), \
             patch("app.daily_report.format_and_send", return_value=True), \
             pytest.raises(SystemExit) as exc_info:
            run_module("app.daily_report", run_name="__main__")
        assert exc_info.value.code == 0

    def test_cli_no_flag_auto_detect(self, tmp_path, monkeypatch):
        """No flag → uses send_daily_report() auto-detect."""
        instance = tmp_path / "instance"
        instance.mkdir()
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))

        with patch("sys.argv", ["daily_report.py"]), \
             patch("app.daily_report.should_send_report", return_value=None), \
             pytest.raises(SystemExit) as exc_info:
            run_module("app.daily_report", run_name="__main__")
        assert exc_info.value.code == 1

    def test_cli_send_failure_exits_1(self, tmp_path, monkeypatch):
        instance = tmp_path / "instance"
        instance.mkdir()
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))

        missions_file = instance / "missions.md"
        missions_file.write_text("# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n\n")
        with patch("sys.argv", ["daily_report.py", "--morning"]), \
             patch("app.notify.format_and_send", return_value=False), \
             patch("app.daily_report.format_and_send", return_value=False), \
             pytest.raises(SystemExit) as exc_info:
            run_module("app.daily_report", run_name="__main__")
        assert exc_info.value.code == 1
