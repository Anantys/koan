"""Tests for reset_parser.py — quota reset time parsing."""

import pytest
from datetime import datetime, timedelta

from tests._helpers import run_module

try:
    import zoneinfo
    ZoneInfo = zoneinfo.ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo


class TestParseResetTime:
    """Test parse_reset_time function."""

    def test_simple_time_am(self):
        """Parse simple 10am format."""
        from app.reset_parser import parse_reset_time

        # Use a fixed "now" to make the test deterministic
        now = datetime(2026, 2, 4, 8, 0, 0, tzinfo=ZoneInfo("Europe/Paris"))
        ts, info = parse_reset_time("resets 10am (Europe/Paris)", now=now)

        assert ts is not None
        reset_dt = datetime.fromtimestamp(ts, tz=ZoneInfo("Europe/Paris"))
        assert reset_dt.hour == 10
        assert reset_dt.minute == 0
        # Should be today since 10am hasn't passed yet at 8am
        assert reset_dt.day == 4
        assert "10am" in info

    def test_simple_time_pm(self):
        """Parse simple 5pm format."""
        from app.reset_parser import parse_reset_time

        now = datetime(2026, 2, 4, 14, 0, 0, tzinfo=ZoneInfo("Europe/Paris"))
        ts, info = parse_reset_time("resets 5pm (Europe/Paris)", now=now)

        assert ts is not None
        reset_dt = datetime.fromtimestamp(ts, tz=ZoneInfo("Europe/Paris"))
        assert reset_dt.hour == 17
        assert "5pm" in info

    def test_time_already_passed_rolls_to_tomorrow(self):
        """If reset time has passed today, should be tomorrow."""
        from app.reset_parser import parse_reset_time

        # It's 3pm, reset is at 10am — should be tomorrow
        now = datetime(2026, 2, 4, 15, 0, 0, tzinfo=ZoneInfo("Europe/Paris"))
        ts, info = parse_reset_time("resets 10am (Europe/Paris)", now=now)

        assert ts is not None
        reset_dt = datetime.fromtimestamp(ts, tz=ZoneInfo("Europe/Paris"))
        assert reset_dt.day == 5  # Tomorrow
        assert reset_dt.hour == 10

    def test_date_with_time(self):
        """Parse 'Feb 5 at 10am' format."""
        from app.reset_parser import parse_reset_time

        now = datetime(2026, 2, 4, 12, 0, 0, tzinfo=ZoneInfo("Europe/Paris"))
        ts, info = parse_reset_time("resets Feb 5 at 10am (Europe/Paris)", now=now)

        assert ts is not None
        reset_dt = datetime.fromtimestamp(ts, tz=ZoneInfo("Europe/Paris"))
        assert reset_dt.month == 2
        assert reset_dt.day == 5
        assert reset_dt.hour == 10
        assert "Feb 5" in info

    def test_tomorrow_format(self):
        """Parse 'tomorrow at 10am' format."""
        from app.reset_parser import parse_reset_time

        now = datetime(2026, 2, 4, 12, 0, 0, tzinfo=ZoneInfo("Europe/Paris"))
        ts, info = parse_reset_time("resets tomorrow at 10am (Europe/Paris)", now=now)

        assert ts is not None
        reset_dt = datetime.fromtimestamp(ts, tz=ZoneInfo("Europe/Paris"))
        assert reset_dt.day == 5  # Tomorrow
        assert reset_dt.hour == 10
        assert "tomorrow" in info

    def test_embedded_in_full_message(self):
        """Parse reset time from full quota exhaustion message."""
        from app.reset_parser import parse_reset_time

        msg = "You're out of extra usage · resets 10am (Europe/Paris)"
        now = datetime(2026, 2, 4, 8, 0, 0, tzinfo=ZoneInfo("Europe/Paris"))
        ts, info = parse_reset_time(msg, now=now)

        assert ts is not None
        assert "10am" in info

    def test_invalid_format_returns_none(self):
        """Unparseable format returns None with original text."""
        from app.reset_parser import parse_reset_time

        ts, info = parse_reset_time("some random text")
        assert ts is None

    def test_in_hours_format(self):
        """Parse 'in Xh' format."""
        from app.reset_parser import parse_reset_time

        now = datetime(2026, 2, 4, 12, 0, 0, tzinfo=ZoneInfo("Europe/Paris"))
        ts, info = parse_reset_time("resets in 3h", now=now)

        assert ts is not None
        reset_dt = datetime.fromtimestamp(ts, tz=ZoneInfo("Europe/Paris"))
        # Should be roughly 3 hours from now
        assert reset_dt.hour == 15
        assert "in 3h" in info


class TestTimeUntilReset:
    """Test time_until_reset function."""

    def test_minutes_only(self):
        """Less than an hour shows minutes."""
        from app.reset_parser import time_until_reset

        now = datetime(2026, 2, 4, 9, 45, 0)
        reset_ts = int((now + timedelta(minutes=15)).timestamp())

        result = time_until_reset(reset_ts, now=now)
        assert result == "15m"

    def test_hours_and_minutes(self):
        """Hours with minutes."""
        from app.reset_parser import time_until_reset

        now = datetime(2026, 2, 4, 8, 0, 0)
        reset_ts = int((now + timedelta(hours=2, minutes=30)).timestamp())

        result = time_until_reset(reset_ts, now=now)
        assert result == "2h 30m"

    def test_hours_only(self):
        """Exact hours."""
        from app.reset_parser import time_until_reset

        now = datetime(2026, 2, 4, 8, 0, 0)
        reset_ts = int((now + timedelta(hours=3)).timestamp())

        result = time_until_reset(reset_ts, now=now)
        assert result == "3h"

    def test_days_and_hours(self):
        """More than 24h shows days."""
        from app.reset_parser import time_until_reset

        now = datetime(2026, 2, 4, 8, 0, 0)
        reset_ts = int((now + timedelta(days=1, hours=5)).timestamp())

        result = time_until_reset(reset_ts, now=now)
        assert result == "1d 5h"

    def test_past_time_returns_now(self):
        """If reset time has passed, return 'now'."""
        from app.reset_parser import time_until_reset

        now = datetime(2026, 2, 4, 12, 0, 0)
        reset_ts = int((now - timedelta(hours=1)).timestamp())

        result = time_until_reset(reset_ts, now=now)
        assert result == "now"


class TestShouldAutoResume:
    """Test should_auto_resume function."""

    def test_before_reset_time(self):
        """Should not resume if current time < reset time."""
        from app.reset_parser import should_auto_resume

        now = datetime(2026, 2, 4, 8, 0, 0)
        reset_ts = int((now + timedelta(hours=2)).timestamp())

        assert should_auto_resume(reset_ts, now=now) is False

    def test_at_reset_time(self):
        """Should resume if current time == reset time."""
        from app.reset_parser import should_auto_resume

        now = datetime(2026, 2, 4, 10, 0, 0)
        reset_ts = int(now.timestamp())

        assert should_auto_resume(reset_ts, now=now) is True

    def test_after_reset_time(self):
        """Should resume if current time > reset time."""
        from app.reset_parser import should_auto_resume

        now = datetime(2026, 2, 4, 12, 0, 0)
        reset_ts = int((now - timedelta(hours=2)).timestamp())

        assert should_auto_resume(reset_ts, now=now) is True


class TestCLIInterface:
    """Test the CLI interface."""

    def test_cli_parse(self, monkeypatch, capsys):
        """Test CLI parse command."""
        import sys
        monkeypatch.setattr(sys, 'argv', ['reset_parser.py', 'parse', 'resets', '5pm', '(Europe/Paris)'])

        from app import reset_parser
        import importlib
        # Re-import to trigger __main__ block... actually let's test the function directly
        ts, info = reset_parser.parse_reset_time("resets 5pm (Europe/Paris)")
        assert ts is not None
        assert "5pm" in info

    def test_cli_until(self):
        """Test CLI until command."""
        from app.reset_parser import time_until_reset
        from datetime import datetime, timedelta

        now = datetime(2026, 2, 4, 8, 0, 0)
        future = int((now + timedelta(hours=2)).timestamp())
        result = time_until_reset(future, now=now)
        assert result == "2h"

    def test_cli_check(self):
        """Test CLI check command."""
        from app.reset_parser import should_auto_resume
        from datetime import datetime, timedelta

        now = datetime(2026, 2, 4, 12, 0, 0)
        past = int((now - timedelta(hours=1)).timestamp())
        assert should_auto_resume(past, now=now) is True


# ---------------------------------------------------------------------------
# Additional edge cases for parse_reset_time
# ---------------------------------------------------------------------------


class TestParseResetTimeEdgeCases:
    """Edge cases and boundary conditions for parse_reset_time."""

    def test_12am_midnight(self):
        """12am should be hour 0 (midnight)."""
        from app.reset_parser import parse_reset_time

        now = datetime(2026, 2, 4, 15, 0, 0, tzinfo=ZoneInfo("Europe/Paris"))
        ts, info = parse_reset_time("resets 12am (Europe/Paris)", now=now)

        assert ts is not None
        reset_dt = datetime.fromtimestamp(ts, tz=ZoneInfo("Europe/Paris"))
        assert reset_dt.hour == 0
        # 12am is past 3pm, so should be tomorrow
        assert reset_dt.day == 5

    def test_12pm_noon(self):
        """12pm should be hour 12 (noon)."""
        from app.reset_parser import parse_reset_time

        now = datetime(2026, 2, 4, 8, 0, 0, tzinfo=ZoneInfo("Europe/Paris"))
        ts, info = parse_reset_time("resets 12pm (Europe/Paris)", now=now)

        assert ts is not None
        reset_dt = datetime.fromtimestamp(ts, tz=ZoneInfo("Europe/Paris"))
        assert reset_dt.hour == 12
        assert reset_dt.day == 4  # Today, since noon hasn't passed

    def test_12pm_already_passed(self):
        """12pm that already passed should roll to tomorrow."""
        from app.reset_parser import parse_reset_time

        now = datetime(2026, 2, 4, 14, 0, 0, tzinfo=ZoneInfo("Europe/Paris"))
        ts, info = parse_reset_time("resets 12pm (Europe/Paris)", now=now)

        assert ts is not None
        reset_dt = datetime.fromtimestamp(ts, tz=ZoneInfo("Europe/Paris"))
        assert reset_dt.hour == 12
        assert reset_dt.day == 5  # Tomorrow

    def test_invalid_timezone_falls_back_to_paris(self):
        """Invalid timezone defaults to Europe/Paris."""
        from app.reset_parser import parse_reset_time

        now = datetime(2026, 2, 4, 8, 0, 0, tzinfo=ZoneInfo("Europe/Paris"))
        ts, info = parse_reset_time("resets 10am (Invalid/Timezone)", now=now)

        assert ts is not None
        reset_dt = datetime.fromtimestamp(ts, tz=ZoneInfo("Europe/Paris"))
        assert reset_dt.hour == 10

    def test_no_timezone_defaults_to_paris(self):
        """Missing timezone defaults to Europe/Paris."""
        from app.reset_parser import parse_reset_time

        now = datetime(2026, 2, 4, 8, 0, 0, tzinfo=ZoneInfo("Europe/Paris"))
        ts, info = parse_reset_time("resets 10am", now=now)

        assert ts is not None
        reset_dt = datetime.fromtimestamp(ts, tz=ZoneInfo("Europe/Paris"))
        assert reset_dt.hour == 10

    def test_date_with_comma_format(self):
        """Parse 'Feb 5, 10am' format (comma separator)."""
        from app.reset_parser import parse_reset_time

        now = datetime(2026, 2, 4, 12, 0, 0, tzinfo=ZoneInfo("Europe/Paris"))
        ts, info = parse_reset_time("resets Feb 5, 10am (Europe/Paris)", now=now)

        assert ts is not None
        reset_dt = datetime.fromtimestamp(ts, tz=ZoneInfo("Europe/Paris"))
        assert reset_dt.month == 2
        assert reset_dt.day == 5
        assert reset_dt.hour == 10

    def test_date_already_passed_this_year_goes_next_year(self):
        """Date in the past this year should roll to next year."""
        from app.reset_parser import parse_reset_time

        # Now is Feb 4, reset is Jan 1 — should be next year
        now = datetime(2026, 2, 4, 12, 0, 0, tzinfo=ZoneInfo("Europe/Paris"))
        ts, info = parse_reset_time("resets Jan 1 at 10am (Europe/Paris)", now=now)

        assert ts is not None
        reset_dt = datetime.fromtimestamp(ts, tz=ZoneInfo("Europe/Paris"))
        assert reset_dt.year == 2027
        assert reset_dt.month == 1
        assert reset_dt.day == 1

    def test_invalid_date_feb_30(self):
        """Invalid date like Feb 30 returns None."""
        from app.reset_parser import parse_reset_time

        now = datetime(2026, 2, 4, 12, 0, 0, tzinfo=ZoneInfo("Europe/Paris"))
        ts, info = parse_reset_time("resets Feb 30 at 10am (Europe/Paris)", now=now)

        assert ts is None

    def test_unknown_month_returns_none(self):
        """Unknown month name returns None."""
        from app.reset_parser import parse_reset_time

        now = datetime(2026, 2, 4, 12, 0, 0, tzinfo=ZoneInfo("Europe/Paris"))
        ts, info = parse_reset_time("resets Xyz 5 at 10am (Europe/Paris)", now=now)

        assert ts is None

    def test_in_hours_with_full_word(self):
        """Parse 'in 5 hours' format."""
        from app.reset_parser import parse_reset_time

        now = datetime(2026, 2, 4, 12, 0, 0, tzinfo=ZoneInfo("Europe/Paris"))
        ts, info = parse_reset_time("resets in 5 hours", now=now)

        assert ts is not None
        reset_dt = datetime.fromtimestamp(ts, tz=ZoneInfo("Europe/Paris"))
        assert reset_dt.hour == 17
        assert "5h" in info

    def test_in_1_hour_singular(self):
        """Parse 'in 1 hour' format (singular)."""
        from app.reset_parser import parse_reset_time

        now = datetime(2026, 2, 4, 12, 0, 0, tzinfo=ZoneInfo("Europe/Paris"))
        ts, info = parse_reset_time("resets in 1 hour", now=now)

        assert ts is not None
        assert "1h" in info

    def test_tomorrow_12pm(self):
        """Parse 'tomorrow at 12pm'."""
        from app.reset_parser import parse_reset_time

        now = datetime(2026, 2, 4, 20, 0, 0, tzinfo=ZoneInfo("Europe/Paris"))
        ts, info = parse_reset_time("resets tomorrow at 12pm (Europe/Paris)", now=now)

        assert ts is not None
        reset_dt = datetime.fromtimestamp(ts, tz=ZoneInfo("Europe/Paris"))
        assert reset_dt.day == 5
        assert reset_dt.hour == 12

    def test_tomorrow_12am(self):
        """Parse 'tomorrow at 12am' (midnight)."""
        from app.reset_parser import parse_reset_time

        now = datetime(2026, 2, 4, 20, 0, 0, tzinfo=ZoneInfo("Europe/Paris"))
        ts, info = parse_reset_time("resets tomorrow at 12am (Europe/Paris)", now=now)

        assert ts is not None
        reset_dt = datetime.fromtimestamp(ts, tz=ZoneInfo("Europe/Paris"))
        assert reset_dt.day == 5
        assert reset_dt.hour == 0

    def test_empty_string(self):
        """Empty string returns None."""
        from app.reset_parser import parse_reset_time

        ts, info = parse_reset_time("")
        assert ts is None

    def test_no_resets_keyword(self):
        """String without 'resets' keyword returns None."""
        from app.reset_parser import parse_reset_time

        ts, info = parse_reset_time("quota exhausted at 10am")
        assert ts is None

    def test_us_timezone(self):
        """Parse with US/Eastern timezone."""
        from app.reset_parser import parse_reset_time

        now = datetime(2026, 2, 4, 8, 0, 0, tzinfo=ZoneInfo("US/Eastern"))
        ts, info = parse_reset_time("resets 10am (US/Eastern)", now=now)

        assert ts is not None
        reset_dt = datetime.fromtimestamp(ts, tz=ZoneInfo("US/Eastern"))
        assert reset_dt.hour == 10

    def test_date_12pm_edge(self):
        """Date format with 12pm."""
        from app.reset_parser import parse_reset_time

        now = datetime(2026, 2, 4, 8, 0, 0, tzinfo=ZoneInfo("Europe/Paris"))
        ts, info = parse_reset_time("resets Feb 5 at 12pm (Europe/Paris)", now=now)

        assert ts is not None
        reset_dt = datetime.fromtimestamp(ts, tz=ZoneInfo("Europe/Paris"))
        assert reset_dt.hour == 12
        assert reset_dt.day == 5

    def test_date_12am_edge(self):
        """Date format with 12am (midnight)."""
        from app.reset_parser import parse_reset_time

        now = datetime(2026, 2, 4, 8, 0, 0, tzinfo=ZoneInfo("Europe/Paris"))
        ts, info = parse_reset_time("resets Feb 5 at 12am (Europe/Paris)", now=now)

        assert ts is not None
        reset_dt = datetime.fromtimestamp(ts, tz=ZoneInfo("Europe/Paris"))
        assert reset_dt.hour == 0
        assert reset_dt.day == 5

    def test_naive_datetime_now(self):
        """Naive datetime (no tzinfo) is handled correctly."""
        from app.reset_parser import parse_reset_time

        # Naive datetime — the function should handle this
        now = datetime(2026, 2, 4, 8, 0, 0)
        ts, info = parse_reset_time("resets 10am (Europe/Paris)", now=now)

        assert ts is not None


class TestParseMonth:
    """Tests for _parse_month helper."""

    def test_all_short_months(self):
        from app.reset_parser import _parse_month
        for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun",
                                "jul", "aug", "sep", "oct", "nov", "dec"], 1):
            assert _parse_month(m) == i

    def test_all_full_months(self):
        from app.reset_parser import _parse_month
        for i, m in enumerate(["january", "february", "march", "april", "may", "june",
                                "july", "august", "september", "october", "november", "december"], 1):
            assert _parse_month(m) == i

    def test_case_insensitive(self):
        from app.reset_parser import _parse_month
        assert _parse_month("JAN") == 1
        assert _parse_month("February") == 2
        assert _parse_month("DEC") == 12

    def test_unknown_month(self):
        from app.reset_parser import _parse_month
        assert _parse_month("xyz") is None
        assert _parse_month("") is None


class TestTimeUntilResetEdgeCases:
    """Additional edge cases for time_until_reset."""

    def test_days_only_no_remaining_hours(self):
        """Exact days should not show hours."""
        from app.reset_parser import time_until_reset

        now = datetime(2026, 2, 4, 8, 0, 0)
        reset_ts = int((now + timedelta(days=3)).timestamp())

        result = time_until_reset(reset_ts, now=now)
        assert result == "3d"

    def test_zero_diff_returns_now(self):
        """Zero difference returns 'now'."""
        from app.reset_parser import time_until_reset

        now = datetime(2026, 2, 4, 10, 0, 0)
        result = time_until_reset(int(now.timestamp()), now=now)
        assert result == "now"

    def test_one_minute(self):
        """Single minute remaining."""
        from app.reset_parser import time_until_reset

        now = datetime(2026, 2, 4, 10, 0, 0)
        reset_ts = int((now + timedelta(minutes=1)).timestamp())

        result = time_until_reset(reset_ts, now=now)
        assert result == "1m"

    def test_exactly_one_hour(self):
        """Exactly one hour remaining."""
        from app.reset_parser import time_until_reset

        now = datetime(2026, 2, 4, 10, 0, 0)
        reset_ts = int((now + timedelta(hours=1)).timestamp())

        result = time_until_reset(reset_ts, now=now)
        assert result == "1h"

    def test_large_time_difference(self):
        """Multi-day difference."""
        from app.reset_parser import time_until_reset

        now = datetime(2026, 2, 4, 10, 0, 0)
        reset_ts = int((now + timedelta(days=7, hours=3)).timestamp())

        result = time_until_reset(reset_ts, now=now)
        assert result == "7d 3h"


class TestCLIMainBlock:
    """Test the CLI __main__ interface via runpy."""

    def test_cli_parse_valid(self):
        """CLI parse command outputs timestamp|info."""
        import sys
        from unittest.mock import patch
        from io import StringIO
        from tests._helpers import run_module

        out = StringIO()
        with patch.object(sys, "argv", ["reset_parser", "parse", "resets 5pm (Europe/Paris)"]):
            with patch("sys.stdout", out):
                try:
                    run_module("app.reset_parser", run_name="__main__")
                except SystemExit:
                    pass

        output = out.getvalue()
        assert "|" in output
        assert "5pm" in output

    def test_cli_parse_empty(self):
        """CLI parse with no text outputs |<text>."""
        import sys
        from unittest.mock import patch
        from io import StringIO
        from tests._helpers import run_module

        out = StringIO()
        with patch.object(sys, "argv", ["reset_parser", "parse"]):
            with patch("sys.stdout", out):
                try:
                    run_module("app.reset_parser", run_name="__main__")
                except SystemExit:
                    pass

        output = out.getvalue()
        assert output.startswith("|")

    def test_cli_check_should_resume(self):
        """CLI check command exits 0 when past reset time."""
        import sys
        from unittest.mock import patch
        from tests._helpers import run_module

        past_ts = str(int(datetime(2020, 1, 1).timestamp()))
        with patch.object(sys, "argv", ["reset_parser", "check", past_ts]):
            with pytest.raises(SystemExit) as exc_info:
                run_module("app.reset_parser", run_name="__main__")
            assert exc_info.value.code == 0

    def test_cli_check_should_not_resume(self):
        """CLI check command exits 1 when before reset time."""
        import sys
        from unittest.mock import patch
        from tests._helpers import run_module

        future_ts = str(int(datetime(2099, 1, 1).timestamp()))
        with patch.object(sys, "argv", ["reset_parser", "check", future_ts]):
            with pytest.raises(SystemExit) as exc_info:
                run_module("app.reset_parser", run_name="__main__")
            assert exc_info.value.code == 1

    def test_cli_check_invalid_value(self):
        """CLI check with invalid value exits 1."""
        import sys
        from unittest.mock import patch
        from tests._helpers import run_module

        with patch.object(sys, "argv", ["reset_parser", "check", "not-a-number"]):
            with pytest.raises(SystemExit) as exc_info:
                run_module("app.reset_parser", run_name="__main__")
            assert exc_info.value.code == 1

    def test_cli_check_no_args(self):
        """CLI check with no timestamp exits 1."""
        import sys
        from unittest.mock import patch
        from tests._helpers import run_module

        with patch.object(sys, "argv", ["reset_parser", "check"]):
            with pytest.raises(SystemExit) as exc_info:
                run_module("app.reset_parser", run_name="__main__")
            assert exc_info.value.code == 1

    def test_cli_until_valid(self):
        """CLI until command outputs human-readable time."""
        import sys
        from unittest.mock import patch
        from io import StringIO
        from tests._helpers import run_module

        future_ts = str(int((datetime.now() + timedelta(hours=2)).timestamp()))
        out = StringIO()
        with patch.object(sys, "argv", ["reset_parser", "until", future_ts]):
            with patch("sys.stdout", out):
                try:
                    run_module("app.reset_parser", run_name="__main__")
                except SystemExit:
                    pass

        output = out.getvalue().strip()
        assert "h" in output or "m" in output

    def test_cli_until_invalid_value(self):
        """CLI until with invalid value outputs 'unknown'."""
        import sys
        from unittest.mock import patch
        from io import StringIO
        from tests._helpers import run_module

        out = StringIO()
        with patch.object(sys, "argv", ["reset_parser", "until", "bad"]):
            with patch("sys.stdout", out):
                try:
                    run_module("app.reset_parser", run_name="__main__")
                except SystemExit:
                    pass

        assert "unknown" in out.getvalue()

    def test_cli_until_no_args(self):
        """CLI until with no args outputs 'unknown'."""
        import sys
        from unittest.mock import patch
        from io import StringIO
        from tests._helpers import run_module

        out = StringIO()
        with patch.object(sys, "argv", ["reset_parser", "until"]):
            with patch("sys.stdout", out):
                try:
                    run_module("app.reset_parser", run_name="__main__")
                except SystemExit:
                    pass

        assert "unknown" in out.getvalue()

    def test_cli_no_args_exits_1(self):
        """CLI with no arguments exits 1."""
        import sys
        from unittest.mock import patch
        from tests._helpers import run_module

        with patch.object(sys, "argv", ["reset_parser"]):
            with pytest.raises(SystemExit) as exc_info:
                run_module("app.reset_parser", run_name="__main__")
            assert exc_info.value.code == 1

    def test_cli_unknown_command_exits_1(self):
        """CLI with unknown command exits 1."""
        import sys
        from unittest.mock import patch
        from tests._helpers import run_module

        with patch.object(sys, "argv", ["reset_parser", "bogus"]):
            with pytest.raises(SystemExit) as exc_info:
                run_module("app.reset_parser", run_name="__main__")
            assert exc_info.value.code == 1
