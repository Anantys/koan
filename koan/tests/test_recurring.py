"""Tests for recurring.py — recurring missions storage and scheduler."""

import json
import pytest
from datetime import datetime, timedelta
from pathlib import Path

from app.recurring import (
    load_recurring,
    save_recurring,
    add_recurring,
    add_recurring_interval,
    remove_recurring,
    list_recurring,
    format_recurring_list,
    is_due,
    check_and_inject,
    parse_at_time,
    parse_interval,
    format_interval,
    FREQUENCIES,
)


# --- load_recurring / save_recurring ---


class TestLoadSave:
    def test_load_nonexistent(self, tmp_path):
        assert load_recurring(tmp_path / "nope.json") == []

    def test_load_empty_file(self, tmp_path):
        f = tmp_path / "recurring.json"
        f.write_text("")
        assert load_recurring(f) == []

    def test_load_invalid_json(self, tmp_path):
        f = tmp_path / "recurring.json"
        f.write_text("{not json")
        assert load_recurring(f) == []

    def test_load_non_list(self, tmp_path):
        f = tmp_path / "recurring.json"
        f.write_text('{"key": "value"}')
        assert load_recurring(f) == []

    def test_roundtrip(self, tmp_path):
        f = tmp_path / "recurring.json"
        data = [{"id": "rec_1", "frequency": "daily", "text": "test"}]
        save_recurring(f, data)
        loaded = load_recurring(f)
        assert loaded == data

    def test_save_creates_file(self, tmp_path):
        f = tmp_path / "recurring.json"
        save_recurring(f, [])
        assert f.exists()
        assert json.loads(f.read_text()) == []


# --- add_recurring ---


class TestAddRecurring:
    def test_add_daily(self, tmp_path):
        f = tmp_path / "recurring.json"
        m = add_recurring(f, "daily", "check emails")
        assert m["frequency"] == "daily"
        assert m["text"] == "check emails"
        assert m["project"] is None
        assert m["last_run"] is None
        assert m["enabled"] is True
        assert m["id"].startswith("rec_")

    def test_add_with_project(self, tmp_path):
        f = tmp_path / "recurring.json"
        m = add_recurring(f, "weekly", "audit security", project="koan")
        assert m["project"] == "koan"

    def test_add_invalid_frequency(self, tmp_path):
        f = tmp_path / "recurring.json"
        with pytest.raises(ValueError, match="Invalid frequency"):
            add_recurring(f, "monthly", "something")

    def test_add_multiple(self, tmp_path):
        f = tmp_path / "recurring.json"
        add_recurring(f, "daily", "task 1")
        add_recurring(f, "hourly", "task 2")
        missions = load_recurring(f)
        assert len(missions) == 2

    def test_add_strips_whitespace(self, tmp_path):
        f = tmp_path / "recurring.json"
        m = add_recurring(f, "daily", "  check emails  ")
        assert m["text"] == "check emails"


# --- remove_recurring ---


class TestRemoveRecurring:
    def _setup(self, tmp_path):
        f = tmp_path / "recurring.json"
        add_recurring(f, "daily", "check emails")
        add_recurring(f, "weekly", "security audit")
        add_recurring(f, "hourly", "check PRs")
        return f

    def test_remove_by_number(self, tmp_path):
        f = self._setup(tmp_path)
        removed = remove_recurring(f, "1")
        assert "check emails" in removed
        assert len(load_recurring(f)) == 2

    def test_remove_by_keyword(self, tmp_path):
        f = self._setup(tmp_path)
        removed = remove_recurring(f, "security")
        assert "security audit" in removed
        assert len(load_recurring(f)) == 2

    def test_remove_invalid_number(self, tmp_path):
        f = self._setup(tmp_path)
        with pytest.raises(ValueError, match="Invalid number"):
            remove_recurring(f, "10")

    def test_remove_no_match(self, tmp_path):
        f = self._setup(tmp_path)
        with pytest.raises(ValueError, match="No recurring mission matching"):
            remove_recurring(f, "nonexistent")

    def test_remove_empty_list(self, tmp_path):
        f = tmp_path / "recurring.json"
        save_recurring(f, [])
        with pytest.raises(ValueError, match="No recurring missions"):
            remove_recurring(f, "1")

    def test_remove_ambiguous(self, tmp_path):
        f = tmp_path / "recurring.json"
        add_recurring(f, "daily", "check emails morning")
        add_recurring(f, "daily", "check emails evening")
        with pytest.raises(ValueError, match="Multiple matches"):
            remove_recurring(f, "check")


# --- list_recurring ---


class TestListRecurring:
    def test_empty(self, tmp_path):
        f = tmp_path / "recurring.json"
        assert list_recurring(f) == []

    def test_sorts_by_frequency(self, tmp_path):
        f = tmp_path / "recurring.json"
        add_recurring(f, "weekly", "task W")
        add_recurring(f, "hourly", "task H")
        add_recurring(f, "daily", "task D")
        result = list_recurring(f)
        assert [m["frequency"] for m in result] == ["hourly", "daily", "weekly"]

    def test_excludes_disabled(self, tmp_path):
        f = tmp_path / "recurring.json"
        missions = [
            {"id": "1", "frequency": "daily", "text": "active", "enabled": True},
            {"id": "2", "frequency": "daily", "text": "disabled", "enabled": False},
        ]
        save_recurring(f, missions)
        result = list_recurring(f)
        assert len(result) == 1
        assert result[0]["text"] == "active"


# --- format_recurring_list ---


class TestFormatRecurringList:
    def test_empty(self):
        assert "No recurring" in format_recurring_list([])

    def test_basic_format(self):
        missions = [
            {"frequency": "daily", "text": "check emails", "project": None, "last_run": None},
        ]
        result = format_recurring_list(missions)
        assert "[daily]" in result
        assert "check emails" in result
        assert "never run" in result

    def test_with_project(self):
        missions = [
            {"frequency": "weekly", "text": "audit", "project": "koan", "last_run": None},
        ]
        result = format_recurring_list(missions)
        assert "project: koan" in result

    def test_with_last_run(self):
        recent = (datetime.now() - timedelta(minutes=30)).isoformat()
        missions = [
            {"frequency": "hourly", "text": "check PRs", "project": None, "last_run": recent},
        ]
        result = format_recurring_list(missions)
        assert "30min ago" in result


# --- is_due ---


class TestIsDue:
    def test_never_run_is_due(self):
        m = {"frequency": "daily", "last_run": None, "enabled": True}
        assert is_due(m) is True

    def test_disabled_not_due(self):
        m = {"frequency": "daily", "last_run": None, "enabled": False}
        assert is_due(m) is False

    def test_hourly_within_hour(self):
        now = datetime(2026, 2, 3, 14, 0)
        m = {"frequency": "hourly", "last_run": (now - timedelta(minutes=30)).isoformat(), "enabled": True}
        assert is_due(m, now) is False

    def test_hourly_past_hour(self):
        now = datetime(2026, 2, 3, 14, 0)
        m = {"frequency": "hourly", "last_run": (now - timedelta(hours=1, minutes=1)).isoformat(), "enabled": True}
        assert is_due(m, now) is True

    def test_daily_same_day(self):
        now = datetime(2026, 2, 3, 14, 0)
        m = {"frequency": "daily", "last_run": datetime(2026, 2, 3, 8, 0).isoformat(), "enabled": True}
        assert is_due(m, now) is False

    def test_daily_next_day(self):
        now = datetime(2026, 2, 4, 8, 0)
        m = {"frequency": "daily", "last_run": datetime(2026, 2, 3, 22, 0).isoformat(), "enabled": True}
        assert is_due(m, now) is True

    def test_weekly_within_week(self):
        now = datetime(2026, 2, 3, 14, 0)
        m = {"frequency": "weekly", "last_run": (now - timedelta(days=3)).isoformat(), "enabled": True}
        assert is_due(m, now) is False

    def test_weekly_past_week(self):
        now = datetime(2026, 2, 10, 14, 0)
        m = {"frequency": "weekly", "last_run": datetime(2026, 2, 3, 8, 0).isoformat(), "enabled": True}
        assert is_due(m, now) is True

    def test_invalid_last_run_is_due(self):
        m = {"frequency": "daily", "last_run": "not-a-date", "enabled": True}
        assert is_due(m) is True


# --- check_and_inject ---


class TestCheckAndInject:
    def _setup_missions(self, tmp_path):
        missions_path = tmp_path / "missions.md"
        missions_path.write_text(
            "# Missions\n\n"
            "## Pending\n\n"
            "## In Progress\n\n"
            "## Done\n\n"
        )
        return missions_path

    def test_no_recurring_file(self, tmp_path):
        missions_path = self._setup_missions(tmp_path)
        recurring_path = tmp_path / "recurring.json"
        result = check_and_inject(recurring_path, missions_path)
        assert result == []

    def test_injects_due_mission(self, tmp_path):
        missions_path = self._setup_missions(tmp_path)
        recurring_path = tmp_path / "recurring.json"
        add_recurring(recurring_path, "daily", "check emails")

        now = datetime(2026, 2, 3, 8, 0)
        result = check_and_inject(recurring_path, missions_path, now)

        assert len(result) == 1
        assert "check emails" in result[0]

        # Verify mission was inserted into missions.md
        content = missions_path.read_text()
        assert "[daily] check emails" in content

        # Verify last_run was updated
        missions = load_recurring(recurring_path)
        assert missions[0]["last_run"] is not None

    def test_skips_not_due(self, tmp_path):
        missions_path = self._setup_missions(tmp_path)
        recurring_path = tmp_path / "recurring.json"

        # Add and mark as recently run
        now = datetime(2026, 2, 3, 14, 0)
        missions_data = [{
            "id": "rec_1",
            "frequency": "daily",
            "text": "check emails",
            "project": None,
            "created": "2026-02-03T08:00:00",
            "last_run": now.isoformat(),
            "enabled": True,
        }]
        save_recurring(recurring_path, missions_data)

        result = check_and_inject(recurring_path, missions_path, now)
        assert result == []

    def test_injects_with_project_tag(self, tmp_path):
        missions_path = self._setup_missions(tmp_path)
        recurring_path = tmp_path / "recurring.json"
        add_recurring(recurring_path, "weekly", "audit security", project="koan")

        now = datetime(2026, 2, 3, 8, 0)
        check_and_inject(recurring_path, missions_path, now)

        content = missions_path.read_text()
        assert "[project:koan]" in content
        assert "[weekly] audit security" in content

    def test_multiple_due(self, tmp_path):
        missions_path = self._setup_missions(tmp_path)
        recurring_path = tmp_path / "recurring.json"
        add_recurring(recurring_path, "daily", "task 1")
        add_recurring(recurring_path, "hourly", "task 2")

        now = datetime(2026, 2, 3, 8, 0)
        result = check_and_inject(recurring_path, missions_path, now)

        assert len(result) == 2

    def test_mixed_due_and_not_due(self, tmp_path):
        missions_path = self._setup_missions(tmp_path)
        recurring_path = tmp_path / "recurring.json"

        now = datetime(2026, 2, 3, 14, 0)
        missions_data = [
            {
                "id": "rec_1", "frequency": "daily", "text": "due task",
                "project": None, "created": "2026-02-01", "last_run": None, "enabled": True,
            },
            {
                "id": "rec_2", "frequency": "daily", "text": "not due",
                "project": None, "created": "2026-02-01",
                "last_run": now.isoformat(), "enabled": True,
            },
        ]
        save_recurring(recurring_path, missions_data)

        result = check_and_inject(recurring_path, missions_path, now)
        assert len(result) == 1
        assert "due task" in result[0]


# --- parse_at_time ---


# --- parse_interval / format_interval ---


class TestParseInterval:
    def test_minutes(self):
        assert parse_interval("5m") == 300

    def test_hours(self):
        assert parse_interval("2h") == 7200

    def test_combined(self):
        assert parse_interval("1h30m") == 5400

    def test_seconds(self):
        assert parse_interval("90s") == 90

    def test_minutes_and_seconds(self):
        assert parse_interval("1m30s") == 90

    def test_minimum_enforced(self):
        with pytest.raises(ValueError, match="Minimum interval"):
            parse_interval("30s")

    def test_invalid_format(self):
        with pytest.raises(ValueError, match="Invalid interval"):
            parse_interval("abc")

    def test_empty_string(self):
        with pytest.raises(ValueError, match="Invalid interval"):
            parse_interval("")

    def test_just_number(self):
        with pytest.raises(ValueError, match="Invalid interval"):
            parse_interval("5")

    def test_case_insensitive(self):
        assert parse_interval("5M") == 300


class TestFormatInterval:
    def test_minutes(self):
        assert format_interval(300) == "5m"

    def test_hours(self):
        assert format_interval(7200) == "2h"

    def test_combined(self):
        assert format_interval(5400) == "1h30m"

    def test_seconds(self):
        assert format_interval(30) == "30s"


# --- add_recurring_interval ---


class TestAddRecurringInterval:
    def test_add_interval(self, tmp_path):
        f = tmp_path / "recurring.json"
        m = add_recurring_interval(f, 300, "5m", "check design", project="nocrm")
        assert m["frequency"] == "every"
        assert m["interval_seconds"] == 300
        assert m["interval_display"] == "5m"
        assert m["text"] == "check design"
        assert m["project"] == "nocrm"

    def test_add_without_project(self, tmp_path):
        f = tmp_path / "recurring.json"
        m = add_recurring_interval(f, 300, "5m", "check health")
        assert m["project"] is None


# --- is_due with every ---


class TestIsDueEvery:
    def test_never_run_is_due(self):
        m = {"frequency": "every", "interval_seconds": 300, "last_run": None, "enabled": True}
        assert is_due(m) is True

    def test_within_interval_not_due(self):
        now = datetime(2026, 2, 3, 14, 0)
        m = {
            "frequency": "every", "interval_seconds": 300,
            "last_run": (now - timedelta(minutes=3)).isoformat(), "enabled": True,
        }
        assert is_due(m, now) is False

    def test_past_interval_due(self):
        now = datetime(2026, 2, 3, 14, 0)
        m = {
            "frequency": "every", "interval_seconds": 300,
            "last_run": (now - timedelta(minutes=6)).isoformat(), "enabled": True,
        }
        assert is_due(m, now) is True

    def test_exact_interval_due(self):
        now = datetime(2026, 2, 3, 14, 0)
        m = {
            "frequency": "every", "interval_seconds": 300,
            "last_run": (now - timedelta(seconds=300)).isoformat(), "enabled": True,
        }
        assert is_due(m, now) is True

    def test_disabled_not_due(self):
        m = {"frequency": "every", "interval_seconds": 300, "last_run": None, "enabled": False}
        assert is_due(m) is False


# --- format_recurring_list with every ---


class TestFormatWithEvery:
    def test_shows_interval(self):
        missions = [
            {"frequency": "every", "interval_seconds": 300, "interval_display": "5m",
             "text": "check design", "project": None, "last_run": None},
        ]
        result = format_recurring_list(missions)
        assert "[every 5m]" in result
        assert "check design" in result


# --- check_and_inject with every ---


class TestCheckAndInjectEvery:
    def _setup_missions(self, tmp_path):
        missions_path = tmp_path / "missions.md"
        missions_path.write_text(
            "# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n\n"
        )
        return missions_path

    def test_injects_due_every_mission(self, tmp_path):
        missions_path = self._setup_missions(tmp_path)
        recurring_path = tmp_path / "recurring.json"
        add_recurring_interval(recurring_path, 300, "5m", "check design", project="nocrm")

        now = datetime(2026, 2, 3, 8, 0)
        result = check_and_inject(recurring_path, missions_path, now)
        assert len(result) == 1
        content = missions_path.read_text()
        assert "[every 5m]" in content
        assert "check design" in content

    def test_skips_within_interval(self, tmp_path):
        missions_path = self._setup_missions(tmp_path)
        recurring_path = tmp_path / "recurring.json"

        now = datetime(2026, 2, 3, 14, 0)
        missions_data = [{
            "id": "rec_1", "frequency": "every", "interval_seconds": 300,
            "interval_display": "5m", "text": "check design", "project": None,
            "created": "2026-02-03T08:00:00",
            "last_run": (now - timedelta(minutes=3)).isoformat(),
            "enabled": True, "at": None,
        }]
        save_recurring(recurring_path, missions_data)

        result = check_and_inject(recurring_path, missions_path, now)
        assert result == []


class TestParseAtTime:
    def test_no_time(self):
        at, text = parse_at_time("check emails")
        assert at is None
        assert text == "check emails"

    def test_valid_time(self):
        at, text = parse_at_time("20:00 check emails")
        assert at == "20:00"
        assert text == "check emails"

    def test_morning_time(self):
        at, text = parse_at_time("8:30 morning task")
        assert at == "08:30"
        assert text == "morning task"

    def test_midnight(self):
        at, text = parse_at_time("0:00 midnight task")
        assert at == "00:00"
        assert text == "midnight task"

    def test_invalid_hour(self):
        with pytest.raises(ValueError, match="Invalid time"):
            parse_at_time("25:00 bad time")

    def test_invalid_minute(self):
        with pytest.raises(ValueError, match="Invalid time"):
            parse_at_time("20:60 bad time")

    def test_strips_whitespace(self):
        at, text = parse_at_time("  14:00   do stuff  ")
        assert at == "14:00"
        assert text == "do stuff"

    def test_bare_number_not_time(self):
        """A number without colon is not parsed as time."""
        at, text = parse_at_time("42 things to do")
        assert at is None
        assert text == "42 things to do"


# --- add_recurring with at ---


class TestAddRecurringWithAt:
    def test_add_with_at(self, tmp_path):
        f = tmp_path / "recurring.json"
        m = add_recurring(f, "daily", "check emails", at="20:00")
        assert m["at"] == "20:00"

    def test_add_without_at(self, tmp_path):
        f = tmp_path / "recurring.json"
        m = add_recurring(f, "daily", "check emails")
        assert m["at"] is None


# --- is_due with at ---


class TestIsDueWithAt:
    def test_daily_at_before_time_not_due(self):
        now = datetime(2026, 2, 4, 18, 0)  # 6pm
        m = {"frequency": "daily", "last_run": datetime(2026, 2, 3, 20, 0).isoformat(),
             "enabled": True, "at": "20:00"}
        assert is_due(m, now) is False  # It's 18:00, not yet 20:00

    def test_daily_at_past_time_due(self):
        now = datetime(2026, 2, 4, 20, 30)  # 8:30pm
        m = {"frequency": "daily", "last_run": datetime(2026, 2, 3, 20, 0).isoformat(),
             "enabled": True, "at": "20:00"}
        assert is_due(m, now) is True  # It's 20:30, past 20:00

    def test_daily_at_exact_time_due(self):
        now = datetime(2026, 2, 4, 20, 0)  # exactly 8pm
        m = {"frequency": "daily", "last_run": datetime(2026, 2, 3, 20, 0).isoformat(),
             "enabled": True, "at": "20:00"}
        assert is_due(m, now) is True

    def test_daily_no_at_still_works(self):
        now = datetime(2026, 2, 4, 8, 0)
        m = {"frequency": "daily", "last_run": datetime(2026, 2, 3, 22, 0).isoformat(),
             "enabled": True}
        assert is_due(m, now) is True  # No at = fires after midnight

    def test_daily_at_already_ran_today(self):
        now = datetime(2026, 2, 4, 21, 0)
        m = {"frequency": "daily", "last_run": datetime(2026, 2, 4, 20, 0).isoformat(),
             "enabled": True, "at": "20:00"}
        assert is_due(m, now) is False  # Already ran today at 20:00

    def test_weekly_at_before_time_not_due(self):
        now = datetime(2026, 2, 10, 18, 0)
        m = {"frequency": "weekly", "last_run": datetime(2026, 2, 3, 20, 0).isoformat(),
             "enabled": True, "at": "20:00"}
        assert is_due(m, now) is False

    def test_weekly_at_past_time_due(self):
        now = datetime(2026, 2, 10, 21, 0)
        m = {"frequency": "weekly", "last_run": datetime(2026, 2, 3, 20, 0).isoformat(),
             "enabled": True, "at": "20:00"}
        assert is_due(m, now) is True

    def test_hourly_ignores_at(self):
        now = datetime(2026, 2, 3, 14, 0)
        m = {"frequency": "hourly",
             "last_run": (now - timedelta(hours=1, minutes=1)).isoformat(),
             "enabled": True, "at": "20:00"}
        assert is_due(m, now) is True  # hourly ignores at

    def test_never_run_with_at_before_time(self):
        now = datetime(2026, 2, 4, 18, 0)
        m = {"frequency": "daily", "last_run": None, "enabled": True, "at": "20:00"}
        assert is_due(m, now) is False  # Never run, but not yet 20:00

    def test_never_run_with_at_past_time(self):
        now = datetime(2026, 2, 4, 21, 0)
        m = {"frequency": "daily", "last_run": None, "enabled": True, "at": "20:00"}
        assert is_due(m, now) is True

    def test_malformed_at_ignored(self):
        now = datetime(2026, 2, 4, 8, 0)
        m = {"frequency": "daily", "last_run": datetime(2026, 2, 3, 22, 0).isoformat(),
             "enabled": True, "at": "bad"}
        assert is_due(m, now) is True  # Malformed at = ignore constraint


# --- format_recurring_list with at ---


class TestFormatWithAt:
    def test_shows_at_time(self):
        missions = [
            {"frequency": "daily", "text": "nightly audit", "project": None,
             "last_run": None, "at": "20:00"},
        ]
        result = format_recurring_list(missions)
        assert "[daily at 20:00]" in result
        assert "nightly audit" in result

    def test_no_at_unchanged(self):
        missions = [
            {"frequency": "daily", "text": "check emails", "project": None,
             "last_run": None, "at": None},
        ]
        result = format_recurring_list(missions)
        assert "[daily]" in result
        assert "at" not in result.split("]")[0].split("[daily")[1]


# --- check_and_inject with at ---


class TestCheckAndInjectWithAt:
    def _setup_missions(self, tmp_path):
        missions_path = tmp_path / "missions.md"
        missions_path.write_text(
            "# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n\n"
        )
        return missions_path

    def test_skips_at_before_time(self, tmp_path):
        missions_path = self._setup_missions(tmp_path)
        recurring_path = tmp_path / "recurring.json"
        add_recurring(recurring_path, "daily", "nightly audit", at="20:00")

        now = datetime(2026, 2, 3, 18, 0)  # 6pm, before 8pm
        result = check_and_inject(recurring_path, missions_path, now)
        assert result == []

    def test_injects_at_past_time(self, tmp_path):
        missions_path = self._setup_missions(tmp_path)
        recurring_path = tmp_path / "recurring.json"
        add_recurring(recurring_path, "daily", "nightly audit", at="20:00")

        now = datetime(2026, 2, 3, 21, 0)  # 9pm, past 8pm
        result = check_and_inject(recurring_path, missions_path, now)
        assert len(result) == 1
        assert "nightly audit" in result[0]


# --- CLI recurring_scheduler.py ---


class TestRecurringSchedulerCLI:
    def test_no_args(self):
        import subprocess
        result = subprocess.run(
            ["python3", "-m", "app.recurring_scheduler"],
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent.parent),
        )
        assert result.returncode == 1

    def test_no_recurring_file(self, tmp_path):
        import subprocess
        result = subprocess.run(
            ["python3", "-m", "app.recurring_scheduler", str(tmp_path)],
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent.parent),
        )
        assert result.returncode == 0
