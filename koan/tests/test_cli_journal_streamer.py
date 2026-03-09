"""Tests for cli_journal_streamer — tail thread, stderr append, lifecycle."""

import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.slow


@pytest.fixture(autouse=True)
def _fast_poll():
    """Patch poll interval from 1.0s to 0.05s for all tests in this module."""
    with patch("app.cli_journal_streamer._POLL_INTERVAL", 0.05):
        yield


@pytest.fixture
def tmp_env(tmp_path):
    """Set up a temp instance directory with journal structure."""
    instance_dir = tmp_path / "instance"
    instance_dir.mkdir()
    (instance_dir / "journal").mkdir()
    return {
        "instance_dir": str(instance_dir),
        "project_name": "test-project",
    }


@pytest.fixture
def stdout_file(tmp_path):
    """Create a temp file for simulated subprocess stdout."""
    path = tmp_path / "stdout.txt"
    path.write_text("")
    return str(path)


@pytest.fixture
def stderr_file(tmp_path):
    """Create a temp file for simulated subprocess stderr."""
    path = tmp_path / "stderr.txt"
    path.write_text("")
    return str(path)


def _journal_content(tmp_env):
    """Read today's journal file if it exists, else return empty string."""
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    path = Path(tmp_env["instance_dir"]) / "journal" / today / "test-project.md"
    return path.read_text() if path.exists() else ""


class TestStartTailThread:
    """Test start_tail_thread / stop_tail_thread lifecycle."""

    def test_streams_new_content_to_journal(self, tmp_env, stdout_file):
        from app.cli_journal_streamer import start_tail_thread, stop_tail_thread

        thread, stop_event = start_tail_thread(
            stdout_file, tmp_env["instance_dir"], tmp_env["project_name"], run_num=1,
        )
        assert thread.is_alive()

        with open(stdout_file, "a") as f:
            f.write("line one\n")
            f.flush()

        time.sleep(0.2)
        stop_tail_thread(thread, stop_event)
        assert not thread.is_alive()

        content = _journal_content(tmp_env)
        assert "CLI Output" in content
        assert "line one" in content

    def test_writes_header_immediately(self, tmp_env, stdout_file):
        from app.cli_journal_streamer import start_tail_thread, stop_tail_thread

        thread, stop_event = start_tail_thread(
            stdout_file, tmp_env["instance_dir"], tmp_env["project_name"], run_num=3,
        )

        time.sleep(0.15)
        content = _journal_content(tmp_env)
        assert "Run 3" in content
        assert "🖥️" in content

        stop_tail_thread(thread, stop_event)

    def test_final_flush_on_stop(self, tmp_env, stdout_file):
        from app.cli_journal_streamer import start_tail_thread, stop_tail_thread

        thread, stop_event = start_tail_thread(
            stdout_file, tmp_env["instance_dir"], tmp_env["project_name"], run_num=1,
        )

        with open(stdout_file, "a") as f:
            f.write("final line\n")
            f.flush()

        stop_tail_thread(thread, stop_event)
        assert "final line" in _journal_content(tmp_env)

    def test_handles_missing_stdout_file(self, tmp_env, tmp_path):
        from app.cli_journal_streamer import start_tail_thread, stop_tail_thread

        nonexistent = str(tmp_path / "does-not-exist.txt")
        thread, stop_event = start_tail_thread(
            nonexistent, tmp_env["instance_dir"], tmp_env["project_name"], run_num=1,
        )
        time.sleep(0.2)
        stop_tail_thread(thread, stop_event)
        assert not thread.is_alive()


class TestAppendStderrToJournal:
    """Test append_stderr_to_journal."""

    def test_appends_stderr_on_error(self, tmp_env, stderr_file):
        from app.cli_journal_streamer import append_stderr_to_journal

        Path(stderr_file).write_text("Error: something went wrong\n")
        append_stderr_to_journal(
            stderr_file, tmp_env["instance_dir"], tmp_env["project_name"], run_num=2,
        )

        content = _journal_content(tmp_env)
        assert "CLI Errors" in content
        assert "Run 2" in content
        assert "something went wrong" in content

    def test_skips_empty_stderr(self, tmp_env, stderr_file):
        from app.cli_journal_streamer import append_stderr_to_journal

        Path(stderr_file).write_text("")
        append_stderr_to_journal(
            stderr_file, tmp_env["instance_dir"], tmp_env["project_name"], run_num=1,
        )
        assert _journal_content(tmp_env) == ""

    def test_skips_missing_stderr_file(self, tmp_env, tmp_path):
        from app.cli_journal_streamer import append_stderr_to_journal

        nonexistent = str(tmp_path / "no-such-file.txt")
        append_stderr_to_journal(
            nonexistent, tmp_env["instance_dir"], tmp_env["project_name"], run_num=1,
        )


class TestJournalStreamLifecycle:
    """Test start_journal_stream / stop_journal_stream high-level helpers."""

    def test_start_returns_handle_when_enabled(self, tmp_env, stdout_file):
        from app.cli_journal_streamer import start_journal_stream, stop_journal_stream

        with patch("app.config._load_config", return_value={"cli_output_journal": True}):
            handle = start_journal_stream(
                stdout_file, tmp_env["instance_dir"], tmp_env["project_name"], run_num=1,
            )
        assert handle is not None

        stop_journal_stream(handle, 0, stdout_file, tmp_env["instance_dir"], tmp_env["project_name"], 1)
        assert "CLI Output" in _journal_content(tmp_env)

    def test_start_returns_none_when_disabled(self, tmp_env, stdout_file):
        from app.cli_journal_streamer import start_journal_stream

        with patch("app.config._load_config", return_value={"cli_output_journal": False}):
            handle = start_journal_stream(
                stdout_file, tmp_env["instance_dir"], tmp_env["project_name"], run_num=1,
            )
        assert handle is None

    def test_stop_is_noop_with_none_handle(self, tmp_env, stderr_file):
        from app.cli_journal_streamer import stop_journal_stream

        # Should not raise
        stop_journal_stream(None, 1, stderr_file, tmp_env["instance_dir"], tmp_env["project_name"], 1)

    def test_stop_appends_stderr_on_nonzero_exit(self, tmp_env, stdout_file, stderr_file):
        from app.cli_journal_streamer import start_journal_stream, stop_journal_stream

        Path(stderr_file).write_text("fatal error\n")

        with patch("app.config._load_config", return_value={"cli_output_journal": True}):
            handle = start_journal_stream(
                stdout_file, tmp_env["instance_dir"], tmp_env["project_name"], run_num=5,
            )

        stop_journal_stream(handle, 1, stderr_file, tmp_env["instance_dir"], tmp_env["project_name"], 5)

        content = _journal_content(tmp_env)
        assert "CLI Errors" in content
        assert "fatal error" in content


class TestConfigOption:
    """Test get_cli_output_journal config function."""

    def test_default_is_true(self):
        from app.config import get_cli_output_journal
        with patch("app.config._load_config", return_value={}):
            assert get_cli_output_journal() is True

    def test_explicit_true(self):
        from app.config import get_cli_output_journal
        with patch("app.config._load_config", return_value={"cli_output_journal": True}):
            assert get_cli_output_journal() is True

    def test_explicit_false(self):
        from app.config import get_cli_output_journal
        with patch("app.config._load_config", return_value={"cli_output_journal": False}):
            assert get_cli_output_journal() is False


# ---------------------------------------------------------------------------
# _decode_safe — UTF-8 split handling
# ---------------------------------------------------------------------------

class TestDecodeSafe:
    """Test _decode_safe() for UTF-8 decoding with incomplete trailing bytes."""

    def test_ascii_string(self):
        from app.cli_journal_streamer import _decode_safe
        text, leftover = _decode_safe(b"hello world")
        assert text == "hello world"
        assert leftover == b""

    def test_empty_bytes(self):
        from app.cli_journal_streamer import _decode_safe
        text, leftover = _decode_safe(b"")
        assert text == ""
        assert leftover == b""

    def test_complete_utf8_multibyte(self):
        from app.cli_journal_streamer import _decode_safe
        # é = \xc3\xa9 (2-byte UTF-8)
        text, leftover = _decode_safe("café".encode("utf-8"))
        assert text == "café"
        assert leftover == b""

    def test_incomplete_2byte_sequence(self):
        from app.cli_journal_streamer import _decode_safe
        # \xc3 is the first byte of é (\xc3\xa9), cut before the second byte
        data = b"caf" + b"\xc3"
        text, leftover = _decode_safe(data)
        assert text == "caf"
        assert leftover == b"\xc3"

    def test_incomplete_3byte_sequence(self):
        from app.cli_journal_streamer import _decode_safe
        # € = \xe2\x82\xac (3-byte UTF-8), cut after 2 bytes
        data = b"price " + b"\xe2\x82"
        text, leftover = _decode_safe(data)
        assert text == "price "
        assert leftover == b"\xe2\x82"

    def test_incomplete_4byte_sequence(self):
        from app.cli_journal_streamer import _decode_safe
        # 🎉 = \xf0\x9f\x8e\x89 (4-byte UTF-8), cut after 1 byte
        data = b"done " + b"\xf0"
        text, leftover = _decode_safe(data)
        assert text == "done "
        assert leftover == b"\xf0"

    def test_complete_emoji(self):
        from app.cli_journal_streamer import _decode_safe
        text, leftover = _decode_safe("🖥️ hello".encode("utf-8"))
        assert "hello" in text
        assert leftover == b""

    def test_only_incomplete_byte(self):
        from app.cli_journal_streamer import _decode_safe
        # Just a single continuation byte — invalid on its own
        data = b"\xc3"
        text, leftover = _decode_safe(data)
        # Should either return leftover or replace
        assert isinstance(text, str)
        assert isinstance(leftover, bytes)

    def test_mixed_valid_and_trailing_incomplete(self):
        from app.cli_journal_streamer import _decode_safe
        # Valid text + incomplete 3-byte at end
        data = "abc".encode("utf-8") + b"\xe2\x82"
        text, leftover = _decode_safe(data)
        assert text == "abc"
        assert leftover == b"\xe2\x82"


# ---------------------------------------------------------------------------
# _journal_write — error handling
# ---------------------------------------------------------------------------

class TestJournalWrite:
    """Test _journal_write error handling."""

    def test_successful_write(self, tmp_env):
        from app.cli_journal_streamer import _journal_write
        _journal_write(Path(tmp_env["instance_dir"]), "test-project", "hello\n")
        assert "hello" in _journal_content(tmp_env)

    def test_exception_logged_to_stderr(self, capsys):
        from app.cli_journal_streamer import _journal_write
        with patch("app.cli_journal_streamer._get_append_fn") as mock_fn:
            mock_fn.return_value = MagicMock(side_effect=OSError("disk full"))
            _journal_write(Path("/tmp/fake"), "proj", "data")
        captured = capsys.readouterr()
        assert "write error" in captured.err
        assert "disk full" in captured.err


# ---------------------------------------------------------------------------
# _tail_loop — internal behavior
# ---------------------------------------------------------------------------

class TestTailLoop:
    """Test _tail_loop internal behavior beyond start/stop lifecycle."""

    def test_incremental_reads_across_polls(self, tmp_env, stdout_file):
        """File grows in multiple bursts; all content is captured."""
        from app.cli_journal_streamer import start_tail_thread, stop_tail_thread

        thread, stop_event = start_tail_thread(
            stdout_file, tmp_env["instance_dir"], tmp_env["project_name"], run_num=1,
        )

        # First burst
        with open(stdout_file, "a") as f:
            f.write("burst one\n")
            f.flush()
        time.sleep(0.2)

        # Second burst
        with open(stdout_file, "a") as f:
            f.write("burst two\n")
            f.flush()
        time.sleep(0.2)

        stop_tail_thread(thread, stop_event)

        content = _journal_content(tmp_env)
        assert "burst one" in content
        assert "burst two" in content

    def test_thread_is_daemon(self, tmp_env, stdout_file):
        from app.cli_journal_streamer import start_tail_thread, stop_tail_thread

        thread, stop_event = start_tail_thread(
            stdout_file, tmp_env["instance_dir"], tmp_env["project_name"], run_num=1,
        )
        assert thread.daemon is True
        assert thread.name == "cli-journal-tail"
        stop_tail_thread(thread, stop_event)

    def test_file_shrinks_between_polls(self, tmp_env, stdout_file):
        """If file is truncated (size < pos), loop waits without crash."""
        from app.cli_journal_streamer import start_tail_thread, stop_tail_thread

        # Write initial content
        with open(stdout_file, "w") as f:
            f.write("initial content that is quite long\n")

        thread, stop_event = start_tail_thread(
            stdout_file, tmp_env["instance_dir"], tmp_env["project_name"], run_num=1,
        )
        time.sleep(0.2)

        # Truncate the file (simulate log rotation)
        with open(stdout_file, "w") as f:
            f.write("")

        time.sleep(0.2)
        stop_tail_thread(thread, stop_event)
        assert not thread.is_alive()

    def test_binary_content_decoded_safely(self, tmp_env, tmp_path):
        """Binary/non-UTF8 content doesn't crash the tail loop."""
        from app.cli_journal_streamer import start_tail_thread, stop_tail_thread

        binfile = str(tmp_path / "binary_out.bin")
        with open(binfile, "wb") as f:
            f.write(b"")

        thread, stop_event = start_tail_thread(
            binfile, tmp_env["instance_dir"], tmp_env["project_name"], run_num=1,
        )

        # Write invalid UTF-8 bytes
        with open(binfile, "ab") as f:
            f.write(b"valid text\xff\xfe more text\n")
            f.flush()

        time.sleep(0.2)
        stop_tail_thread(thread, stop_event)

        content = _journal_content(tmp_env)
        assert "valid text" in content


# ---------------------------------------------------------------------------
# append_stderr_to_journal — edge cases
# ---------------------------------------------------------------------------

class TestAppendStderrEdgeCases:
    """Additional edge cases for append_stderr_to_journal."""

    def test_whitespace_only_stderr_skipped(self, tmp_env, stderr_file):
        from app.cli_journal_streamer import append_stderr_to_journal

        Path(stderr_file).write_text("   \n  \n")
        append_stderr_to_journal(
            stderr_file, tmp_env["instance_dir"], tmp_env["project_name"], run_num=1,
        )
        assert _journal_content(tmp_env) == ""

    def test_stderr_content_wrapped_in_code_block(self, tmp_env, stderr_file):
        from app.cli_journal_streamer import append_stderr_to_journal

        Path(stderr_file).write_text("traceback line 1\ntraceback line 2\n")
        append_stderr_to_journal(
            stderr_file, tmp_env["instance_dir"], tmp_env["project_name"], run_num=7,
        )
        content = _journal_content(tmp_env)
        assert "```" in content
        assert "traceback line 1" in content

    def test_stderr_run_num_in_header(self, tmp_env, stderr_file):
        from app.cli_journal_streamer import append_stderr_to_journal

        Path(stderr_file).write_text("error\n")
        append_stderr_to_journal(
            stderr_file, tmp_env["instance_dir"], tmp_env["project_name"], run_num=42,
        )
        content = _journal_content(tmp_env)
        assert "Run 42" in content


# ---------------------------------------------------------------------------
# Lifecycle edge cases
# ---------------------------------------------------------------------------

class TestLifecycleEdgeCases:
    """Edge cases for start/stop journal stream lifecycle."""

    def test_start_exception_returns_none(self, tmp_env, stdout_file, capsys):
        from app.cli_journal_streamer import start_journal_stream

        with patch("app.cli_journal_streamer.start_tail_thread", side_effect=RuntimeError("boom")):
            with patch("app.config._load_config", return_value={"cli_output_journal": True}):
                handle = start_journal_stream(
                    stdout_file, tmp_env["instance_dir"], tmp_env["project_name"], run_num=1,
                )
        assert handle is None
        captured = capsys.readouterr()
        assert "start error" in captured.err

    def test_stop_exception_caught(self, tmp_env, stderr_file, capsys):
        from app.cli_journal_streamer import stop_journal_stream

        mock_thread = MagicMock()
        mock_event = MagicMock()
        mock_thread.join.side_effect = RuntimeError("join failed")

        stop_journal_stream(
            (mock_thread, mock_event), 0, stderr_file,
            tmp_env["instance_dir"], tmp_env["project_name"], 1,
        )
        captured = capsys.readouterr()
        assert "stop error" in captured.err

    def test_stop_does_not_append_stderr_on_zero_exit(self, tmp_env, stdout_file, stderr_file):
        from app.cli_journal_streamer import start_journal_stream, stop_journal_stream

        Path(stderr_file).write_text("some warning\n")

        with patch("app.config._load_config", return_value={"cli_output_journal": True}):
            handle = start_journal_stream(
                stdout_file, tmp_env["instance_dir"], tmp_env["project_name"], run_num=1,
            )

        stop_journal_stream(handle, 0, stderr_file, tmp_env["instance_dir"], tmp_env["project_name"], 1)

        content = _journal_content(tmp_env)
        assert "CLI Errors" not in content
        assert "some warning" not in content

    def test_stop_tail_thread_with_short_timeout(self, tmp_env, stdout_file):
        from app.cli_journal_streamer import start_tail_thread, stop_tail_thread

        thread, stop_event = start_tail_thread(
            stdout_file, tmp_env["instance_dir"], tmp_env["project_name"], run_num=1,
        )
        # Even with very short timeout, should not raise
        stop_tail_thread(thread, stop_event, timeout=0.1)

    def test_stop_tail_thread_sets_event(self):
        from app.cli_journal_streamer import stop_tail_thread

        mock_thread = MagicMock()
        stop_event = threading.Event()

        stop_tail_thread(mock_thread, stop_event)
        assert stop_event.is_set()
        mock_thread.join.assert_called_once()


# ---------------------------------------------------------------------------
# _get_append_fn — lazy import
# ---------------------------------------------------------------------------

class TestGetAppendFn:
    """Test _get_append_fn lazy import."""

    def test_returns_callable(self):
        from app.cli_journal_streamer import _get_append_fn
        fn = _get_append_fn()
        assert callable(fn)

    def test_returns_append_to_journal(self):
        from app.cli_journal_streamer import _get_append_fn
        from app.journal import append_to_journal
        assert _get_append_fn() is append_to_journal
