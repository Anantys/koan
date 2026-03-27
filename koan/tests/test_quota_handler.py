"""Tests for quota_handler.py — quota exhaustion detection and handling."""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


class TestDetectQuotaExhaustion:
    """Test detect_quota_exhaustion function."""

    def test_detects_out_of_extra_usage(self):
        from app.quota_handler import detect_quota_exhaustion

        assert detect_quota_exhaustion("Error: out of extra usage") is True

    def test_detects_quota_reached(self):
        from app.quota_handler import detect_quota_exhaustion

        assert detect_quota_exhaustion("Your quota has been reached") is True

    def test_detects_rate_limit(self):
        from app.quota_handler import detect_quota_exhaustion

        assert detect_quota_exhaustion("rate limit exceeded") is True

    def test_case_insensitive(self):
        from app.quota_handler import detect_quota_exhaustion

        assert detect_quota_exhaustion("OUT OF EXTRA USAGE") is True
        assert detect_quota_exhaustion("Rate Limit") is True

    def test_no_match_on_normal_output(self):
        from app.quota_handler import detect_quota_exhaustion

        assert detect_quota_exhaustion("Mission completed successfully") is False

    def test_no_match_on_empty_string(self):
        from app.quota_handler import detect_quota_exhaustion

        assert detect_quota_exhaustion("") is False

    def test_quota_in_longer_text(self):
        from app.quota_handler import detect_quota_exhaustion

        text = """Some normal output here
Error: You have run out of extra usage for claude-opus-4-20250514.
Your quota resets 10am (Europe/Paris)."""
        assert detect_quota_exhaustion(text) is True


class TestDetectQuotaExhaustionCopilot:
    """Test detect_quota_exhaustion with Copilot/GitHub-style messages."""

    def test_detects_too_many_requests(self):
        from app.quota_handler import detect_quota_exhaustion

        assert detect_quota_exhaustion("Error: too many requests") is True

    def test_detects_usage_limit(self):
        from app.quota_handler import detect_quota_exhaustion

        assert detect_quota_exhaustion("You've reached your usage limit") is True

    def test_detects_exceeded_copilot_rate(self):
        from app.quota_handler import detect_quota_exhaustion

        assert detect_quota_exhaustion("You have exceeded a secondary rate limit") is True
        assert detect_quota_exhaustion("exceeded copilot rate limit") is True

    def test_detects_copilot_not_available(self):
        from app.quota_handler import detect_quota_exhaustion

        assert detect_quota_exhaustion("Copilot is not available for this account") is True
        assert detect_quota_exhaustion("Copilot unavailable") is True

    def test_detects_http_429(self):
        from app.quota_handler import detect_quota_exhaustion

        assert detect_quota_exhaustion("HTTP 429 Too Many Requests") is True
        assert detect_quota_exhaustion("status: 429") is True

    def test_detects_retry_after(self):
        from app.quota_handler import detect_quota_exhaustion

        assert detect_quota_exhaustion("Retry-After: 60") is True
        assert detect_quota_exhaustion("retry after 120") is True

    def test_copilot_in_longer_text(self):
        from app.quota_handler import detect_quota_exhaustion

        text = """Error running copilot agent:
API returned HTTP 429: too many requests.
Retry-After: 300
Please try again later."""
        assert detect_quota_exhaustion(text) is True

    def test_no_false_positive_on_copilot_normal_output(self):
        from app.quota_handler import detect_quota_exhaustion

        assert detect_quota_exhaustion("Copilot completed the task successfully") is False
        assert detect_quota_exhaustion("Using copilot provider for mission") is False


class TestExtractResetInfo:
    """Test extract_reset_info function."""

    def test_extracts_resets_with_timezone(self):
        from app.quota_handler import extract_reset_info

        text = "Your quota resets 10am (Europe/Paris)"
        assert "resets 10am (Europe/Paris)" in extract_reset_info(text)

    def test_extracts_reset_with_date(self):
        from app.quota_handler import extract_reset_info

        text = "resets Feb 4 at 10am (Europe/Paris)"
        result = extract_reset_info(text)
        assert "resets" in result
        assert "Feb 4" in result

    def test_returns_empty_on_no_match(self):
        from app.quota_handler import extract_reset_info

        assert extract_reset_info("no reset info here") == ""

    def test_returns_empty_on_empty_string(self):
        from app.quota_handler import extract_reset_info

        assert extract_reset_info("") == ""

    def test_extracts_from_multiline(self):
        from app.quota_handler import extract_reset_info

        text = """Error: out of extra usage
resets 5pm (US/Eastern)
Please try again later."""
        result = extract_reset_info(text)
        assert "resets 5pm" in result


class TestExtractResetInfoCopilot:
    """Test extract_reset_info with Copilot/GitHub-style retry info."""

    def test_extracts_retry_after_seconds(self):
        from app.quota_handler import extract_reset_info

        text = "Error: rate limit exceeded\nRetry-After: 300"
        result = extract_reset_info(text)
        assert "resets in 5m" == result

    def test_extracts_retry_after_large_seconds(self):
        from app.quota_handler import extract_reset_info

        text = "Retry-After: 3600"
        result = extract_reset_info(text)
        assert "resets in 1h" == result

    def test_extracts_retry_after_seconds_with_remainder(self):
        from app.quota_handler import extract_reset_info

        text = "Retry-After: 5400"
        result = extract_reset_info(text)
        assert "resets in 1h 30m" == result

    def test_extracts_try_again_in_minutes(self):
        from app.quota_handler import extract_reset_info

        text = "Rate limited. try again in 15 minutes"
        result = extract_reset_info(text)
        assert "resets in 15m" == result

    def test_extracts_try_again_in_90_minutes(self):
        """Regression: 90 minutes was truncated to '1h' instead of '1h 30m'."""
        from app.quota_handler import extract_reset_info

        text = "Rate limited. try again in 90 minutes"
        result = extract_reset_info(text)
        assert result == "resets in 1h 30m"

    def test_extracts_try_again_in_65_minutes(self):
        from app.quota_handler import extract_reset_info

        text = "try again in 65 minutes"
        result = extract_reset_info(text)
        assert result == "resets in 1h 5m"

    def test_extracts_try_again_in_120_minutes(self):
        from app.quota_handler import extract_reset_info

        text = "try again in 120 minutes"
        result = extract_reset_info(text)
        assert result == "resets in 2h"

    def test_extracts_try_again_in_60_minutes(self):
        from app.quota_handler import extract_reset_info

        text = "try again in 60 minutes"
        result = extract_reset_info(text)
        assert result == "resets in 1h"

    def test_extracts_try_again_in_hours(self):
        from app.quota_handler import extract_reset_info

        text = "Usage limit. try again in 2 hours"
        result = extract_reset_info(text)
        assert "resets in 2h" == result

    def test_extracts_try_again_in_seconds(self):
        from app.quota_handler import extract_reset_info

        text = "try again in 30 seconds"
        result = extract_reset_info(text)
        assert "resets in 30s" == result

    def test_claude_style_takes_precedence_over_retry_after(self):
        from app.quota_handler import extract_reset_info

        text = "resets 10am (Europe/Paris)\nRetry-After: 300"
        result = extract_reset_info(text)
        assert "resets 10am" in result

    def test_retry_after_small_value(self):
        from app.quota_handler import extract_reset_info

        text = "Retry-After: 45"
        result = extract_reset_info(text)
        assert "resets in 45s" == result


class TestExtractResetInfoBoundsChecking:
    """Test bounds checking in extract_reset_info — zero/negative/huge values."""

    def test_retry_after_zero_defaults_to_1h(self):
        from app.quota_handler import extract_reset_info

        text = "Retry-After: 0"
        result = extract_reset_info(text)
        assert result == "resets in 1h"

    def test_retry_after_huge_value_capped_to_24h(self):
        from app.quota_handler import extract_reset_info

        text = "Retry-After: 999999"
        result = extract_reset_info(text)
        assert result == "resets in 24h"

    def test_retry_after_exactly_86400_not_capped(self):
        from app.quota_handler import extract_reset_info

        text = "Retry-After: 86400"
        result = extract_reset_info(text)
        assert result == "resets in 24h"

    def test_retry_after_86401_capped(self):
        from app.quota_handler import extract_reset_info

        text = "Retry-After: 86401"
        result = extract_reset_info(text)
        assert result == "resets in 24h"

    def test_try_again_in_0_minutes_defaults_to_1h(self):
        from app.quota_handler import extract_reset_info

        text = "try again in 0 minutes"
        result = extract_reset_info(text)
        assert result == "resets in 1h"

    def test_try_again_in_huge_hours_capped(self):
        from app.quota_handler import extract_reset_info

        text = "try again in 100 hours"
        result = extract_reset_info(text)
        assert result == "resets in 24h"

    def test_try_again_in_0_seconds_defaults_to_1h(self):
        from app.quota_handler import extract_reset_info

        text = "try again in 0 seconds"
        result = extract_reset_info(text)
        assert result == "resets in 1h"

    def test_try_again_in_2000_minutes_capped(self):
        from app.quota_handler import extract_reset_info

        text = "try again in 2000 minutes"
        result = extract_reset_info(text)
        assert result == "resets in 24h"

    def test_huge_minutes_clamped_before_multiply(self):
        """Regression: raw value must be clamped before multiplying by 60.

        A malformed header like 'try again in 99999 minutes' should not
        produce a multi-million-second intermediate value. The raw minutes
        value should be capped to 1440 (24h) before conversion.
        """
        from app.quota_handler import extract_reset_info

        text = "try again in 99999 minutes"
        result = extract_reset_info(text)
        assert result == "resets in 24h"

    def test_huge_hours_clamped_before_multiply(self):
        """Regression: raw value must be clamped before multiplying by 3600."""
        from app.quota_handler import extract_reset_info

        text = "try again in 500 hours"
        result = extract_reset_info(text)
        assert result == "resets in 24h"


class TestClampRetrySeconds:
    """Test _clamp_retry_seconds helper directly."""

    def test_zero_returns_default(self):
        from app.quota_handler import _clamp_retry_seconds

        assert _clamp_retry_seconds(0) == 3600

    def test_negative_returns_default(self):
        from app.quota_handler import _clamp_retry_seconds

        assert _clamp_retry_seconds(-10) == 3600

    def test_normal_value_unchanged(self):
        from app.quota_handler import _clamp_retry_seconds

        assert _clamp_retry_seconds(300) == 300

    def test_max_boundary(self):
        from app.quota_handler import _clamp_retry_seconds

        assert _clamp_retry_seconds(86400) == 86400

    def test_above_max_capped(self):
        from app.quota_handler import _clamp_retry_seconds

        assert _clamp_retry_seconds(100000) == 86400


class TestSecondsToHuman:
    """Test _seconds_to_human helper."""

    def test_seconds_only(self):
        from app.quota_handler import _seconds_to_human

        assert _seconds_to_human(30) == "30s"
        assert _seconds_to_human(59) == "59s"

    def test_minutes_only(self):
        from app.quota_handler import _seconds_to_human

        assert _seconds_to_human(60) == "1m"
        assert _seconds_to_human(300) == "5m"

    def test_hours_only(self):
        from app.quota_handler import _seconds_to_human

        assert _seconds_to_human(3600) == "1h"
        assert _seconds_to_human(7200) == "2h"

    def test_hours_and_minutes(self):
        from app.quota_handler import _seconds_to_human

        assert _seconds_to_human(5400) == "1h 30m"
        assert _seconds_to_human(3660) == "1h 1m"

    def test_zero(self):
        from app.quota_handler import _seconds_to_human

        assert _seconds_to_human(0) == "0s"


class TestParseResetTime:
    """Test parse_reset_time delegation to reset_parser."""

    def test_delegates_to_reset_parser(self):
        from app.quota_handler import parse_reset_time

        ts, display = parse_reset_time("resets 10am (Europe/Paris)")
        # Should return a valid timestamp (non-None)
        assert ts is not None
        assert isinstance(ts, int)
        assert "10am" in display

    def test_handles_unparseable_input(self):
        from app.quota_handler import parse_reset_time

        ts, display = parse_reset_time("garbage text")
        assert ts is None

    def test_handles_empty_input(self):
        from app.quota_handler import parse_reset_time

        ts, display = parse_reset_time("")
        assert ts is None


class TestComputeResumeInfo:
    """Test compute_resume_info function."""

    def test_with_valid_timestamp(self):
        from app.quota_handler import compute_resume_info

        # A timestamp 2 hours from now
        import time

        future_ts = int(time.time()) + 7200
        effective_ts, msg = compute_resume_info(future_ts, "resets 10am")
        assert effective_ts == future_ts
        assert "Auto-resume at reset time" in msg

    def test_with_none_timestamp_uses_fallback(self):
        from app.quota_handler import compute_resume_info
        from app.pause_manager import QUOTA_RETRY_SECONDS

        import time

        before = int(time.time()) + QUOTA_RETRY_SECONDS - 10
        effective_ts, msg = compute_resume_info(None, "unknown")
        after = int(time.time()) + QUOTA_RETRY_SECONDS + 10
        assert before <= effective_ts <= after
        assert "1h" in msg
        assert "reset time unknown" in msg


class TestWriteQuotaJournal:
    """Test write_quota_journal function."""

    def test_creates_journal_entry(self, tmp_path):
        from app.quota_handler import write_quota_journal

        instance = str(tmp_path / "instance")
        os.makedirs(instance)

        write_quota_journal(instance, "koan", 5, "resets 10am", "Auto-resume in 2h")

        from datetime import date

        journal_dir = os.path.join(instance, "journal", date.today().strftime("%Y-%m-%d"))
        journal_file = os.path.join(journal_dir, "koan.md")
        assert os.path.isfile(journal_file)

        content = Path(journal_file).read_text()
        assert "Quota Exhausted" in content
        assert "5 runs" in content
        assert "koan" in content
        assert "resets 10am" in content
        assert "Auto-resume in 2h" in content

    def test_appends_to_existing_journal(self, tmp_path):
        from app.quota_handler import write_quota_journal

        instance = str(tmp_path / "instance")
        from datetime import date

        journal_dir = os.path.join(instance, "journal", date.today().strftime("%Y-%m-%d"))
        os.makedirs(journal_dir)
        journal_file = os.path.join(journal_dir, "koan.md")
        with open(journal_file, "w") as f:
            f.write("## Previous entry\n\nSome content.\n")

        write_quota_journal(instance, "koan", 3, "resets 5pm", "Auto-resume later")

        content = Path(journal_file).read_text()
        assert "Previous entry" in content
        assert "Quota Exhausted" in content

    def test_creates_journal_directory_if_needed(self, tmp_path):
        from app.quota_handler import write_quota_journal

        instance = str(tmp_path / "instance")
        # Don't create journal dir — should be auto-created
        os.makedirs(instance)

        write_quota_journal(instance, "myproject", 1, "info", "msg")

        from datetime import date

        journal_dir = os.path.join(instance, "journal", date.today().strftime("%Y-%m-%d"))
        assert os.path.isdir(journal_dir)

    @patch("app.journal.append_to_journal")
    def test_uses_append_to_journal_for_locking(self, mock_append, tmp_path):
        """Verify write_quota_journal uses append_to_journal (which has file locking)."""
        from app.quota_handler import write_quota_journal

        instance = str(tmp_path / "instance")
        os.makedirs(instance)

        write_quota_journal(instance, "koan", 5, "resets 10am", "Auto-resume in 2h")

        mock_append.assert_called_once()
        args = mock_append.call_args
        assert args[0][1] == "koan"  # project_name
        assert "Quota Exhausted" in args[0][2]  # content


class TestHandleQuotaExhaustion:
    """Test handle_quota_exhaustion — the main orchestrator."""

    def test_returns_none_when_no_quota_issue(self, tmp_path):
        from app.quota_handler import handle_quota_exhaustion

        stdout_file = str(tmp_path / "stdout")
        stderr_file = str(tmp_path / "stderr")
        with open(stdout_file, "w") as f:
            f.write("Mission completed successfully.")
        with open(stderr_file, "w") as f:
            f.write("")

        instance = str(tmp_path / "instance")
        os.makedirs(instance)

        result = handle_quota_exhaustion(
            str(tmp_path), instance, "koan", 5, stdout_file, stderr_file
        )
        assert result is None

    def test_detects_quota_in_stderr(self, tmp_path):
        from app.quota_handler import handle_quota_exhaustion

        stdout_file = str(tmp_path / "stdout")
        stderr_file = str(tmp_path / "stderr")
        with open(stdout_file, "w") as f:
            f.write("Some output")
        with open(stderr_file, "w") as f:
            f.write("Error: out of extra usage. resets 10am (Europe/Paris)")

        instance = str(tmp_path / "instance")
        os.makedirs(instance)

        result = handle_quota_exhaustion(
            str(tmp_path), instance, "koan", 5, stdout_file, stderr_file
        )
        assert result is not None
        reset_display, resume_msg = result
        assert "10am" in reset_display
        assert "Auto-resume" in resume_msg

    def test_detects_quota_in_stdout(self, tmp_path):
        from app.quota_handler import handle_quota_exhaustion

        stdout_file = str(tmp_path / "stdout")
        stderr_file = str(tmp_path / "stderr")
        with open(stdout_file, "w") as f:
            f.write("Your quota has been reached")
        with open(stderr_file, "w") as f:
            f.write("")

        instance = str(tmp_path / "instance")
        os.makedirs(instance)

        result = handle_quota_exhaustion(
            str(tmp_path), instance, "koan", 3, stdout_file, stderr_file
        )
        assert result is not None

    def test_creates_pause_files(self, tmp_path):
        from app.quota_handler import handle_quota_exhaustion

        stdout_file = str(tmp_path / "stdout")
        stderr_file = str(tmp_path / "stderr")
        with open(stdout_file, "w") as f:
            f.write("out of extra usage resets 10am (Europe/Paris)")
        with open(stderr_file, "w") as f:
            f.write("")

        instance = str(tmp_path / "instance")
        os.makedirs(instance)

        handle_quota_exhaustion(
            str(tmp_path), instance, "koan", 5, stdout_file, stderr_file
        )

        assert os.path.isfile(str(tmp_path / ".koan-pause"))

        pause_content = (tmp_path / ".koan-pause").read_text()
        assert "quota" in pause_content

    def test_writes_journal_entry(self, tmp_path):
        from app.quota_handler import handle_quota_exhaustion

        stdout_file = str(tmp_path / "stdout")
        stderr_file = str(tmp_path / "stderr")
        with open(stdout_file, "w") as f:
            f.write("")
        # "rate limit" is a loose pattern — must be in stderr to trigger
        with open(stderr_file, "w") as f:
            f.write("rate limit exceeded resets 5pm (Europe/Paris)")

        instance = str(tmp_path / "instance")
        os.makedirs(instance)

        handle_quota_exhaustion(
            str(tmp_path), instance, "myproject", 7, stdout_file, stderr_file
        )

        from datetime import date

        journal_dir = os.path.join(instance, "journal", date.today().strftime("%Y-%m-%d"))
        journal_file = os.path.join(journal_dir, "myproject.md")
        assert os.path.isfile(journal_file)
        content = Path(journal_file).read_text()
        assert "7 runs" in content

    def test_handles_missing_stdout_file(self, tmp_path):
        from app.quota_handler import handle_quota_exhaustion

        stderr_file = str(tmp_path / "stderr")
        with open(stderr_file, "w") as f:
            f.write("out of extra usage")

        instance = str(tmp_path / "instance")
        os.makedirs(instance)

        # stdout file doesn't exist — should still detect quota from stderr
        result = handle_quota_exhaustion(
            str(tmp_path), instance, "koan", 1,
            str(tmp_path / "nonexistent"), stderr_file
        )
        assert result is not None

    def test_handles_missing_stderr_file(self, tmp_path):
        from app.quota_handler import handle_quota_exhaustion

        stdout_file = str(tmp_path / "stdout")
        with open(stdout_file, "w") as f:
            f.write("quota reached")

        instance = str(tmp_path / "instance")
        os.makedirs(instance)

        result = handle_quota_exhaustion(
            str(tmp_path), instance, "koan", 1,
            stdout_file, str(tmp_path / "nonexistent")
        )
        assert result is not None

    def test_handles_both_files_missing(self, tmp_path, capsys):
        from app.quota_handler import handle_quota_exhaustion, QUOTA_CHECK_UNRELIABLE

        instance = str(tmp_path / "instance")
        os.makedirs(instance)

        result = handle_quota_exhaustion(
            str(tmp_path), instance, "koan", 1,
            str(tmp_path / "nonexistent1"), str(tmp_path / "nonexistent2")
        )
        assert result is QUOTA_CHECK_UNRELIABLE
        assert result is not None  # callers using `is not None` won't confuse it with "no quota"

        # Should warn when both files are unreadable
        captured = capsys.readouterr()
        assert "WARNING" in captured.err
        assert "quota check unreliable" in captured.err

    def test_fallback_when_no_reset_time(self, tmp_path):
        from app.quota_handler import handle_quota_exhaustion

        stdout_file = str(tmp_path / "stdout")
        stderr_file = str(tmp_path / "stderr")
        # Quota signal but no parseable reset time
        with open(stdout_file, "w") as f:
            f.write("out of extra usage. Try again later.")
        with open(stderr_file, "w") as f:
            f.write("")

        instance = str(tmp_path / "instance")
        os.makedirs(instance)

        result = handle_quota_exhaustion(
            str(tmp_path), instance, "koan", 2, stdout_file, stderr_file
        )
        assert result is not None
        _, resume_msg = result
        assert "1h" in resume_msg

    def test_pause_reason_is_quota(self, tmp_path):
        from app.quota_handler import handle_quota_exhaustion
        from app.pause_manager import get_pause_state

        stdout_file = str(tmp_path / "stdout")
        stderr_file = str(tmp_path / "stderr")
        with open(stdout_file, "w") as f:
            f.write("out of extra usage resets 10am (Europe/Paris)")
        with open(stderr_file, "w") as f:
            f.write("")

        instance = str(tmp_path / "instance")
        os.makedirs(instance)

        handle_quota_exhaustion(
            str(tmp_path), instance, "koan", 5, stdout_file, stderr_file
        )

        state = get_pause_state(str(tmp_path))
        assert state is not None
        assert state.reason == "quota"
        assert state.is_quota is True


class TestStdoutFalsePositives:
    """Test that loose quota patterns in stdout don't trigger false positives.

    Claude's response text (stdout) may legitimately discuss API rate limiting,
    retry-after headers, etc. Only strict patterns (actual CLI error messages)
    should trigger from stdout.  Loose patterns should only match in stderr.

    This class was added after a real incident where a /plan mission discussing
    "rate limit" in an API design triggered a false positive quota pause.
    """

    def test_rate_limit_in_plan_text_does_not_trigger(self, tmp_path):
        """Repro for the original bug: plan text mentioning rate limiting."""
        from app.quota_handler import handle_quota_exhaustion

        stdout_file = str(tmp_path / "stdout")
        stderr_file = str(tmp_path / "stderr")
        # This is Claude's response discussing API rate limiting — NOT an error
        with open(stdout_file, "w") as f:
            f.write(
                "- **CMC returns `None` fields** (API partial response or "
                "rate limit): Skip all threshold checks for that ticker."
            )
        with open(stderr_file, "w") as f:
            f.write("")

        instance = str(tmp_path / "instance")
        os.makedirs(instance)

        result = handle_quota_exhaustion(
            str(tmp_path), instance, "koan", 13, stdout_file, stderr_file
        )
        assert result is None, "Loose pattern 'rate limit' in stdout should not trigger"

    def test_retry_after_in_code_review_does_not_trigger(self, tmp_path):
        from app.quota_handler import handle_quota_exhaustion

        stdout_file = str(tmp_path / "stdout")
        stderr_file = str(tmp_path / "stderr")
        with open(stdout_file, "w") as f:
            f.write("The API should return a Retry-After header when throttled.")
        with open(stderr_file, "w") as f:
            f.write("")

        instance = str(tmp_path / "instance")
        os.makedirs(instance)

        result = handle_quota_exhaustion(
            str(tmp_path), instance, "koan", 5, stdout_file, stderr_file
        )
        assert result is None, "Loose pattern 'retry-after' in stdout should not trigger"

    def test_http_429_in_code_does_not_trigger(self, tmp_path):
        from app.quota_handler import handle_quota_exhaustion

        stdout_file = str(tmp_path / "stdout")
        stderr_file = str(tmp_path / "stderr")
        with open(stdout_file, "w") as f:
            f.write("Handle HTTP 429 responses with exponential backoff.")
        with open(stderr_file, "w") as f:
            f.write("")

        instance = str(tmp_path / "instance")
        os.makedirs(instance)

        result = handle_quota_exhaustion(
            str(tmp_path), instance, "koan", 5, stdout_file, stderr_file
        )
        assert result is None, "Loose pattern 'HTTP 429' in stdout should not trigger"

    def test_too_many_requests_in_docs_does_not_trigger(self, tmp_path):
        from app.quota_handler import handle_quota_exhaustion

        stdout_file = str(tmp_path / "stdout")
        stderr_file = str(tmp_path / "stderr")
        with open(stdout_file, "w") as f:
            f.write("Returns 'too many requests' when the rate limit is exceeded.")
        with open(stderr_file, "w") as f:
            f.write("")

        instance = str(tmp_path / "instance")
        os.makedirs(instance)

        result = handle_quota_exhaustion(
            str(tmp_path), instance, "koan", 5, stdout_file, stderr_file
        )
        assert result is None, "Loose pattern 'too many requests' in stdout should not trigger"

    def test_strict_pattern_in_stdout_still_triggers(self, tmp_path):
        """Strict patterns like 'out of extra usage' are safe in stdout."""
        from app.quota_handler import handle_quota_exhaustion

        stdout_file = str(tmp_path / "stdout")
        stderr_file = str(tmp_path / "stderr")
        with open(stdout_file, "w") as f:
            f.write("Error: out of extra usage. resets 10am (Europe/Paris)")
        with open(stderr_file, "w") as f:
            f.write("")

        instance = str(tmp_path / "instance")
        os.makedirs(instance)

        result = handle_quota_exhaustion(
            str(tmp_path), instance, "koan", 5, stdout_file, stderr_file
        )
        assert result is not None, "Strict pattern in stdout should still trigger"

    def test_loose_pattern_in_stderr_triggers(self, tmp_path):
        """Loose patterns in stderr (actual CLI errors) should still trigger."""
        from app.quota_handler import handle_quota_exhaustion

        stdout_file = str(tmp_path / "stdout")
        stderr_file = str(tmp_path / "stderr")
        with open(stdout_file, "w") as f:
            f.write("Some normal output")
        with open(stderr_file, "w") as f:
            f.write("Error: rate limit exceeded")

        instance = str(tmp_path / "instance")
        os.makedirs(instance)

        result = handle_quota_exhaustion(
            str(tmp_path), instance, "koan", 5, stdout_file, stderr_file
        )
        assert result is not None, "Loose pattern in stderr should trigger"

    def test_loose_pattern_in_stderr_with_content_stdout(self, tmp_path):
        """Stderr rate limit should trigger even if stdout has normal content."""
        from app.quota_handler import handle_quota_exhaustion

        stdout_file = str(tmp_path / "stdout")
        stderr_file = str(tmp_path / "stderr")
        with open(stdout_file, "w") as f:
            f.write("Plan: implement rate limiting for the API\n"
                    "Step 1: Add retry-after headers")
        with open(stderr_file, "w") as f:
            f.write("HTTP 429 Too Many Requests")

        instance = str(tmp_path / "instance")
        os.makedirs(instance)

        result = handle_quota_exhaustion(
            str(tmp_path), instance, "koan", 5, stdout_file, stderr_file
        )
        assert result is not None, "Stderr quota error should trigger regardless of stdout"


class TestCLI:
    """Test CLI interface for run.py integration."""

    def test_cli_detects_quota(self, tmp_path):
        stdout_file = str(tmp_path / "stdout")
        stderr_file = str(tmp_path / "stderr")
        with open(stdout_file, "w") as f:
            f.write("out of extra usage resets 10am (Europe/Paris)")
        with open(stderr_file, "w") as f:
            f.write("")

        instance = str(tmp_path / "instance")
        os.makedirs(instance)

        result = subprocess.run(
            [sys.executable, "-m", "app.quota_handler", "check",
             str(tmp_path), instance, "koan", "5", stdout_file, stderr_file],
            capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": os.path.join(os.path.dirname(__file__), "..")}
        )
        assert result.returncode == 0
        output = result.stdout.strip()
        assert "|" in output
        parts = output.split("|")
        assert len(parts) == 2
        assert "10am" in parts[0]
        assert "Auto-resume" in parts[1]

    def test_cli_exits_1_when_no_quota(self, tmp_path):
        stdout_file = str(tmp_path / "stdout")
        stderr_file = str(tmp_path / "stderr")
        with open(stdout_file, "w") as f:
            f.write("Mission completed successfully")
        with open(stderr_file, "w") as f:
            f.write("")

        instance = str(tmp_path / "instance")
        os.makedirs(instance)

        result = subprocess.run(
            [sys.executable, "-m", "app.quota_handler", "check",
             str(tmp_path), instance, "koan", "5", stdout_file, stderr_file],
            capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": os.path.join(os.path.dirname(__file__), "..")}
        )
        assert result.returncode == 1

    def test_cli_handles_invalid_run_count(self, tmp_path):
        stdout_file = str(tmp_path / "stdout")
        stderr_file = str(tmp_path / "stderr")
        with open(stdout_file, "w") as f:
            f.write("out of extra usage")
        with open(stderr_file, "w") as f:
            f.write("")

        instance = str(tmp_path / "instance")
        os.makedirs(instance)

        result = subprocess.run(
            [sys.executable, "-m", "app.quota_handler", "check",
             str(tmp_path), instance, "koan", "not_a_number", stdout_file, stderr_file],
            capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": os.path.join(os.path.dirname(__file__), "..")}
        )
        # Should still work with fallback run_count=0
        assert result.returncode == 0

    def test_cli_unknown_command(self, tmp_path):
        result = subprocess.run(
            [sys.executable, "-m", "app.quota_handler", "unknown"],
            capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": os.path.join(os.path.dirname(__file__), "..")}
        )
        assert result.returncode == 1

    def test_cli_exits_2_when_both_files_missing(self, tmp_path):
        instance = str(tmp_path / "instance")
        os.makedirs(instance)

        result = subprocess.run(
            [sys.executable, "-m", "app.quota_handler", "check",
             str(tmp_path), instance, "koan", "5",
             str(tmp_path / "nope1"), str(tmp_path / "nope2")],
            capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": os.path.join(os.path.dirname(__file__), "..")}
        )
        assert result.returncode == 2
        assert "UNRELIABLE" in result.stderr

    def test_cli_missing_args(self):
        result = subprocess.run(
            [sys.executable, "-m", "app.quota_handler", "check"],
            capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": os.path.join(os.path.dirname(__file__), "..")}
        )
        assert result.returncode == 1
