"""Tests for health_check.py — bridge and run loop heartbeat monitoring."""

import time
from unittest.mock import patch

import pytest

from app.health_check import (
    write_heartbeat, check_heartbeat, check_and_alert,
    write_run_heartbeat, check_run_heartbeat, get_run_heartbeat_age,
)


class TestWriteHeartbeat:

    def test_creates_file(self, tmp_path):
        write_heartbeat(str(tmp_path))
        hb = tmp_path / ".koan-heartbeat"
        assert hb.exists()
        ts = float(hb.read_text().strip())
        assert abs(ts - time.time()) < 2

    def test_overwrites(self, tmp_path):
        write_heartbeat(str(tmp_path))
        first = float((tmp_path / ".koan-heartbeat").read_text())
        time.sleep(0.01)
        write_heartbeat(str(tmp_path))
        second = float((tmp_path / ".koan-heartbeat").read_text())
        assert second >= first


class TestCheckHeartbeat:

    def test_no_file_is_healthy(self, tmp_path):
        assert check_heartbeat(str(tmp_path)) is True

    def test_fresh_heartbeat(self, tmp_path):
        write_heartbeat(str(tmp_path))
        assert check_heartbeat(str(tmp_path), max_age=60) is True

    def test_stale_heartbeat(self, tmp_path):
        hb = tmp_path / ".koan-heartbeat"
        hb.write_text(str(time.time() - 120))
        assert check_heartbeat(str(tmp_path), max_age=60) is False

    def test_corrupt_file(self, tmp_path):
        hb = tmp_path / ".koan-heartbeat"
        hb.write_text("not a number")
        assert check_heartbeat(str(tmp_path)) is False

    def test_exact_boundary(self, tmp_path):
        hb = tmp_path / ".koan-heartbeat"
        hb.write_text(str(time.time() - 59))
        assert check_heartbeat(str(tmp_path), max_age=60) is True


class TestCheckAndAlert:

    @patch("app.health_check.format_and_send")
    def test_healthy_no_alert(self, mock_send, tmp_path):
        write_heartbeat(str(tmp_path))
        result = check_and_alert(str(tmp_path))
        assert result is True
        mock_send.assert_not_called()

    @patch("app.health_check.format_and_send")
    def test_stale_sends_alert(self, mock_send, tmp_path):
        hb = tmp_path / ".koan-heartbeat"
        hb.write_text(str(time.time() - 300))
        result = check_and_alert(str(tmp_path), max_age=60)
        assert result is False
        mock_send.assert_called_once()
        assert "down" in mock_send.call_args[0][0]

    @patch("app.health_check.format_and_send")
    def test_corrupt_sends_alert(self, mock_send, tmp_path):
        hb = tmp_path / ".koan-heartbeat"
        hb.write_text("garbage")
        result = check_and_alert(str(tmp_path), max_age=60)
        assert result is False
        mock_send.assert_called_once()
        assert "unreadable" in mock_send.call_args[0][0]

    @patch("app.health_check.format_and_send")
    def test_no_file_no_alert(self, mock_send, tmp_path):
        result = check_and_alert(str(tmp_path))
        assert result is True
        mock_send.assert_not_called()


class TestHealthCheckCLI:
    """Tests for __main__ CLI entry point (lines 73-93)."""

    def test_cli_healthy(self, tmp_path, monkeypatch):
        from tests._helpers import run_module; import io, contextlib
        write_heartbeat(str(tmp_path))
        monkeypatch.setattr("sys.argv", ["health_check.py", str(tmp_path)])
        f = io.StringIO()
        with patch("app.notify.format_and_send"), \
             patch("app.health_check.format_and_send"), \
             contextlib.redirect_stdout(f), \
             pytest.raises(SystemExit) as exc_info:
            run_module("app.health_check", run_name="__main__")
        assert exc_info.value.code == 0
        assert "healthy" in f.getvalue()

    def test_cli_stale(self, tmp_path, monkeypatch):
        from tests._helpers import run_module; import io, contextlib
        hb = tmp_path / ".koan-heartbeat"
        hb.write_text(str(time.time() - 300))
        monkeypatch.setattr("sys.argv", ["health_check.py", str(tmp_path), "--max-age", "60"])
        f = io.StringIO()
        with patch("app.notify.format_and_send"), \
             patch("app.health_check.format_and_send"), \
             contextlib.redirect_stdout(f), \
             pytest.raises(SystemExit) as exc_info:
            run_module("app.health_check", run_name="__main__")
        assert exc_info.value.code == 1

    def test_cli_no_args(self, monkeypatch):
        from tests._helpers import run_module
        monkeypatch.setattr("sys.argv", ["health_check.py"])
        with pytest.raises(SystemExit) as exc_info:
            run_module("app.health_check", run_name="__main__")
        assert exc_info.value.code == 2

    def test_cli_invalid_max_age(self, tmp_path, monkeypatch):
        from tests._helpers import run_module
        monkeypatch.setattr("sys.argv", ["health_check.py", str(tmp_path), "--max-age", "abc"])
        with pytest.raises(SystemExit) as exc_info:
            run_module("app.health_check", run_name="__main__")
        assert exc_info.value.code == 2

    def test_cli_reports_run_heartbeat(self, tmp_path, monkeypatch):
        from tests._helpers import run_module; import io, contextlib
        write_heartbeat(str(tmp_path))
        write_run_heartbeat(str(tmp_path))
        monkeypatch.setattr("sys.argv", ["health_check.py", str(tmp_path)])
        f = io.StringIO()
        with patch("app.notify.format_and_send"), \
             patch("app.health_check.format_and_send"), \
             contextlib.redirect_stdout(f), \
             pytest.raises(SystemExit) as exc_info:
            run_module("app.health_check", run_name="__main__")
        assert exc_info.value.code == 0
        output = f.getvalue()
        assert "Run loop" in output

    def test_cli_stale_run_heartbeat(self, tmp_path, monkeypatch):
        from tests._helpers import run_module; import io, contextlib
        write_heartbeat(str(tmp_path))
        # Write a stale run heartbeat
        hb = tmp_path / ".koan-run-heartbeat"
        hb.write_text(str(time.time() - 1200))
        monkeypatch.setattr("sys.argv", ["health_check.py", str(tmp_path)])
        f = io.StringIO()
        with patch("app.notify.format_and_send"), \
             patch("app.health_check.format_and_send"), \
             contextlib.redirect_stdout(f), \
             pytest.raises(SystemExit) as exc_info:
            run_module("app.health_check", run_name="__main__")
        assert exc_info.value.code == 1
        assert "STALE" in f.getvalue()


class TestRunHeartbeat:

    def test_write_creates_file(self, tmp_path):
        write_run_heartbeat(str(tmp_path))
        hb = tmp_path / ".koan-run-heartbeat"
        assert hb.exists()
        ts = float(hb.read_text().strip())
        assert abs(ts - time.time()) < 2

    def test_fresh_heartbeat(self, tmp_path):
        write_run_heartbeat(str(tmp_path))
        assert check_run_heartbeat(str(tmp_path)) is True

    def test_stale_heartbeat(self, tmp_path):
        hb = tmp_path / ".koan-run-heartbeat"
        hb.write_text(str(time.time() - 1200))
        assert check_run_heartbeat(str(tmp_path), max_age=600) is False

    def test_no_file_is_healthy(self, tmp_path):
        assert check_run_heartbeat(str(tmp_path)) is True

    def test_corrupt_file(self, tmp_path):
        hb = tmp_path / ".koan-run-heartbeat"
        hb.write_text("garbage")
        assert check_run_heartbeat(str(tmp_path)) is False

    def test_get_age_no_file(self, tmp_path):
        assert get_run_heartbeat_age(str(tmp_path)) == -1

    def test_get_age_fresh(self, tmp_path):
        write_run_heartbeat(str(tmp_path))
        age = get_run_heartbeat_age(str(tmp_path))
        assert 0 <= age < 2

    def test_get_age_corrupt(self, tmp_path):
        hb = tmp_path / ".koan-run-heartbeat"
        hb.write_text("garbage")
        assert get_run_heartbeat_age(str(tmp_path)) == -1
