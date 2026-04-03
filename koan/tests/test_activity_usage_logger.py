"""Tests for app.activity_usage_logger."""

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _reset_logger():
    """Reset the module-level logger between tests."""
    from app.activity_usage_logger import reset
    reset()
    yield
    reset()


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    """Set KOAN_ROOT to a temp dir and return the logs directory."""
    monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
    return tmp_path / "logs"


class TestLogActivityUsage:
    def test_creates_log_file_and_writes_entry(self, log_dir):
        from app.activity_usage_logger import log_activity_usage

        log_activity_usage(
            project="myproject",
            activity_type="mission",
            description="Fix the bug",
            duration_seconds=342,
            input_tokens=12000,
            output_tokens=4500,
        )

        log_file = log_dir / "usage.log"
        assert log_file.exists()
        content = log_file.read_text()
        assert "myproject" in content
        assert "mission" in content
        assert "Fix the bug" in content
        assert "12.0k" in content  # input tokens
        assert "4.5k" in content   # output tokens
        assert "5m42s" in content  # duration

    def test_includes_cost_when_provided(self, log_dir):
        from app.activity_usage_logger import log_activity_usage

        log_activity_usage(
            project="proj",
            activity_type="mission",
            description="Costly task",
            input_tokens=50000,
            output_tokens=20000,
            cost_usd=0.1234,
        )

        content = (log_dir / "usage.log").read_text()
        assert "$0.1234" in content

    def test_includes_model_short_name(self, log_dir):
        from app.activity_usage_logger import log_activity_usage

        log_activity_usage(
            project="proj",
            activity_type="mission",
            model="claude-sonnet-4-20250514",
            input_tokens=1000,
            output_tokens=500,
        )

        content = (log_dir / "usage.log").read_text()
        assert "sonnet-4" in content
        # Should NOT contain the full model name
        assert "claude-sonnet" not in content

    def test_includes_cache_info_when_present(self, log_dir):
        from app.activity_usage_logger import log_activity_usage

        log_activity_usage(
            project="proj",
            activity_type="mission",
            input_tokens=5000,
            output_tokens=2000,
            cache_read_tokens=3000,
            cache_creation_tokens=1000,
        )

        content = (log_dir / "usage.log").read_text()
        assert "cache:" in content
        assert "3.0k read" in content
        assert "1.0k created" in content

    def test_no_cache_info_when_zero(self, log_dir):
        from app.activity_usage_logger import log_activity_usage

        log_activity_usage(
            project="proj",
            activity_type="mission",
            input_tokens=5000,
            output_tokens=2000,
        )

        content = (log_dir / "usage.log").read_text()
        assert "cache:" not in content

    def test_multiple_entries_appended(self, log_dir):
        from app.activity_usage_logger import log_activity_usage

        log_activity_usage(project="p1", activity_type="mission",
                           input_tokens=100, output_tokens=50)
        log_activity_usage(project="p2", activity_type="contemplative",
                           input_tokens=200, output_tokens=100)

        lines = (log_dir / "usage.log").read_text().strip().split("\n")
        assert len(lines) == 2
        assert "p1" in lines[0]
        assert "p2" in lines[1]

    def test_creates_logs_directory(self, log_dir):
        from app.activity_usage_logger import log_activity_usage

        assert not log_dir.exists()
        log_activity_usage(project="proj", activity_type="test",
                           input_tokens=10, output_tokens=5)
        assert log_dir.exists()
        assert (log_dir / "usage.log").exists()


class TestFormatDuration:
    def test_seconds_only(self):
        from app.activity_usage_logger import _format_duration
        assert _format_duration(45) == "45s"

    def test_minutes_and_seconds(self):
        from app.activity_usage_logger import _format_duration
        assert _format_duration(342) == "5m42s"

    def test_hours(self):
        from app.activity_usage_logger import _format_duration
        assert _format_duration(3723) == "1h02m"

    def test_zero(self):
        from app.activity_usage_logger import _format_duration
        assert _format_duration(0) == "0s"


class TestFormatTokens:
    def test_small(self):
        from app.activity_usage_logger import _format_tokens
        assert _format_tokens(500) == "500"

    def test_thousands(self):
        from app.activity_usage_logger import _format_tokens
        assert _format_tokens(12000) == "12.0k"

    def test_millions(self):
        from app.activity_usage_logger import _format_tokens
        assert _format_tokens(1_500_000) == "1.5M"
