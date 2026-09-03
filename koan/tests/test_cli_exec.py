"""Tests for app.cli_exec — secure prompt passing via temp files."""

import os
import subprocess
from unittest.mock import patch, MagicMock

import pytest

from app.cli_exec import (
    STDIN_PLACEHOLDER,
    _uses_stdin_passing,
    prepare_prompt_file,
    run_cli,
    popen_cli,
    stream_with_timeout,
    _cleanup_prompt_file,
)
from app.provider.claude import ClaudeProvider
from app.provider.codex import CodexProvider
from app.provider.copilot import CopilotProvider
from app.provider.ollama_launch import OllamaLaunchProvider


# ---------------------------------------------------------------------------
# _uses_stdin_passing
# ---------------------------------------------------------------------------

class TestUsesStdinPassing:
    """Tests for _uses_stdin_passing() provider detection."""

    @patch("app.provider.get_provider", return_value=ClaudeProvider())
    def test_claude_provider_uses_stdin(self, _mock):
        assert _uses_stdin_passing() is True

    @patch("app.provider.get_provider", return_value=CopilotProvider())
    def test_copilot_provider_skips_stdin(self, _mock):
        assert _uses_stdin_passing() is False

    @patch("app.provider.get_provider", return_value=OllamaLaunchProvider())
    def test_ollama_launch_provider_uses_stdin(self, _mock):
        assert _uses_stdin_passing() is True

    @patch("app.provider.get_provider", side_effect=ImportError("no provider"))
    def test_import_error_defaults_to_true(self, _mock):
        assert _uses_stdin_passing() is True

    @patch("app.provider.get_provider", side_effect=RuntimeError("broken"))
    def test_runtime_error_defaults_to_true(self, _mock):
        assert _uses_stdin_passing() is True


# ---------------------------------------------------------------------------
# prepare_prompt_file
# ---------------------------------------------------------------------------

class TestPreparePromptFile:
    """Tests for prepare_prompt_file()."""

    def test_extracts_prompt_and_writes_temp_file(self):
        cmd = ["claude", "-p", "my secret prompt", "--model", "opus"]
        new_cmd, path, _use_stdin = prepare_prompt_file(cmd)
        try:
            assert path is not None
            assert os.path.isfile(path)
            assert new_cmd == ["claude", "-p", STDIN_PLACEHOLDER, "--model", "opus"]
            with open(path) as f:
                assert f.read() == "my secret prompt"
            # Check permissions are restrictive
            mode = os.stat(path).st_mode & 0o777
            assert mode == 0o600
        finally:
            _cleanup_prompt_file(path)

    def test_no_p_flag_returns_unchanged(self):
        cmd = ["claude", "--model", "opus"]
        new_cmd, path, _use_stdin = prepare_prompt_file(cmd)
        assert new_cmd is cmd
        assert path is None

    def test_p_at_end_with_no_value_returns_unchanged(self):
        cmd = ["claude", "-p"]
        new_cmd, path, _use_stdin = prepare_prompt_file(cmd)
        assert new_cmd is cmd
        assert path is None

    def test_already_placeholder_returns_none(self):
        cmd = ["claude", "-p", STDIN_PLACEHOLDER, "--model", "opus"]
        new_cmd, path, _use_stdin = prepare_prompt_file(cmd)
        assert new_cmd is cmd
        assert path is None

    def test_preserves_original_cmd(self):
        cmd = ["claude", "-p", "secret"]
        original = cmd.copy()
        new_cmd, path, _use_stdin = prepare_prompt_file(cmd)
        try:
            assert cmd == original  # original not mutated
            assert new_cmd is not cmd
        finally:
            _cleanup_prompt_file(path)

    def test_handles_unicode_prompt(self):
        cmd = ["claude", "-p", "日本語のプロンプト 🎯"]
        new_cmd, path, _use_stdin = prepare_prompt_file(cmd)
        try:
            with open(path, encoding="utf-8") as f:
                assert f.read() == "日本語のプロンプト 🎯"
        finally:
            _cleanup_prompt_file(path)

    def test_handles_empty_prompt(self):
        cmd = ["claude", "-p", ""]
        new_cmd, path, _use_stdin = prepare_prompt_file(cmd)
        try:
            assert path is not None
            with open(path) as f:
                assert f.read() == ""
            assert new_cmd[2] == STDIN_PLACEHOLDER
        finally:
            _cleanup_prompt_file(path)

    def test_copilot_gh_mode(self):
        cmd = ["gh", "copilot", "-p", "my prompt", "--model", "opus"]
        new_cmd, path, _use_stdin = prepare_prompt_file(cmd)
        try:
            assert new_cmd == ["gh", "copilot", "-p", STDIN_PLACEHOLDER, "--model", "opus"]
            with open(path) as f:
                assert f.read() == "my prompt"
        finally:
            _cleanup_prompt_file(path)

    @patch("app.provider.get_provider", return_value=CopilotProvider())
    def test_copilot_provider_skips_stdin_passing(self, _mock):
        """Copilot provider should skip @stdin mechanism entirely."""
        cmd = ["copilot", "-p", "my prompt", "--allow-all-tools"]
        new_cmd, path, _use_stdin = prepare_prompt_file(cmd)
        assert new_cmd is cmd
        assert path is None

    @patch("app.provider.get_provider", return_value=CodexProvider())
    def test_codex_exec_prompt_uses_stdin_dash(self, _mock):
        """Codex exec reads '-' from stdin, so the prompt stays out of argv."""
        cmd = ["codex", "exec", "--sandbox", "workspace-write", "my prompt"]
        new_cmd, path, _use_stdin = prepare_prompt_file(cmd)
        try:
            assert path is not None
            assert new_cmd == ["codex", "exec", "--sandbox", "workspace-write", "-"]
            with open(path) as f:
                assert f.read() == "my prompt"
            mode = os.stat(path).st_mode & 0o777
            assert mode == 0o600
        finally:
            _cleanup_prompt_file(path)

    @patch("app.provider.get_provider", return_value=CodexProvider())
    def test_codex_large_prompt_removed_from_argv(self, _mock):
        """Regression for OSError: Argument list too long when using Codex."""
        prompt = "x" * 200_000
        cmd = ["codex", "exec", "--json", prompt]
        new_cmd, path, _use_stdin = prepare_prompt_file(cmd)
        try:
            assert new_cmd == ["codex", "exec", "--json", "-"]
            assert prompt not in new_cmd
            with open(path) as f:
                assert f.read() == prompt
        finally:
            _cleanup_prompt_file(path)

    @patch("app.provider.get_provider", return_value=CodexProvider())
    def test_codex_existing_stdin_dash_returns_unchanged(self, _mock):
        cmd = ["codex", "exec", "--json", "-"]
        new_cmd, path, _use_stdin = prepare_prompt_file(cmd)
        assert new_cmd is cmd
        assert path is None

    @patch("app.provider.get_provider", return_value=CodexProvider())
    def test_codex_without_prompt_returns_unchanged(self, _mock):
        cmd = ["codex", "exec", "--json"]
        new_cmd, path, _use_stdin = prepare_prompt_file(cmd)
        assert new_cmd is cmd
        assert path is None


# ---------------------------------------------------------------------------
# _cleanup_prompt_file
# ---------------------------------------------------------------------------

class TestCleanupPromptFile:

    def test_removes_existing_file(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("data")
        _cleanup_prompt_file(str(f))
        assert not f.exists()

    def test_ignores_none(self):
        _cleanup_prompt_file(None)  # should not raise

    def test_ignores_missing_file(self):
        _cleanup_prompt_file("/nonexistent/path/file.md")  # should not raise


# ---------------------------------------------------------------------------
# run_cli
# ---------------------------------------------------------------------------

class TestRunCli:

    @patch("app.cli_exec.subprocess.run")
    def test_passes_prompt_via_stdin_fd(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess([], 0, "ok", "")
        cmd = ["claude", "-p", "secret prompt", "--model", "opus"]

        result = run_cli(cmd, capture_output=True, text=True, timeout=60)

        call_args = mock_run.call_args
        actual_cmd = call_args[0][0]
        assert actual_cmd[2] == STDIN_PLACEHOLDER
        assert "secret prompt" not in actual_cmd
        # stdin should be a file object, not DEVNULL
        assert call_args[1]["stdin"] != subprocess.DEVNULL

    @patch("app.cli_exec.subprocess.run")
    def test_falls_back_to_devnull_without_p(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess([], 0, "ok", "")
        cmd = ["git", "status"]

        run_cli(cmd, capture_output=True, text=True)

        call_args = mock_run.call_args
        assert call_args[1]["stdin"] == subprocess.DEVNULL

    @patch("app.cli_exec.subprocess.run")
    def test_cleans_up_temp_file_on_success(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess([], 0, "ok", "")
        cmd = ["claude", "-p", "test prompt"]

        import glob
        from app.utils import koan_tmp_dir
        pat = os.path.join(koan_tmp_dir(), "koan-prompt-*")
        before = set(glob.glob(pat))
        run_cli(cmd, capture_output=True, text=True)
        after = set(glob.glob(pat))
        assert after - before == set()

    @patch("app.cli_exec.subprocess.run", side_effect=Exception("boom"))
    def test_cleans_up_temp_file_on_exception(self, mock_run):
        cmd = ["claude", "-p", "test prompt"]

        import glob
        from app.utils import koan_tmp_dir
        pat = os.path.join(koan_tmp_dir(), "koan-prompt-*")
        before = set(glob.glob(pat))
        with pytest.raises(Exception, match="boom"):
            run_cli(cmd, capture_output=True, text=True)
        after = set(glob.glob(pat))
        assert after - before == set()

    @patch("app.cli_exec.subprocess.run")
    def test_removes_existing_stdin_kwarg(self, mock_run):
        """If caller passes stdin=DEVNULL, it gets replaced with the file."""
        mock_run.return_value = subprocess.CompletedProcess([], 0, "ok", "")
        cmd = ["claude", "-p", "prompt"]

        run_cli(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True)

        call_args = mock_run.call_args
        assert call_args[1]["stdin"] != subprocess.DEVNULL

    @patch("app.provider.get_provider", return_value=CopilotProvider())
    @patch("app.cli_exec.subprocess.run")
    def test_copilot_keeps_prompt_in_args(self, mock_run, _mock_provider):
        """Copilot provider: prompt stays in -p, stdin is DEVNULL."""
        mock_run.return_value = subprocess.CompletedProcess([], 0, "ok", "")
        cmd = ["copilot", "-p", "my prompt", "--model", "opus"]

        run_cli(cmd, capture_output=True, text=True)

        actual_cmd = mock_run.call_args[0][0]
        assert actual_cmd == ["copilot", "-p", "my prompt", "--model", "opus"]
        assert mock_run.call_args[1]["stdin"] == subprocess.DEVNULL

    @patch("app.cli_exec.subprocess.run")
    def test_default_timeout_applied_when_missing(self, mock_run):
        """run_cli applies DEFAULT_TIMEOUT when caller omits timeout=."""
        from app.cli_exec import DEFAULT_TIMEOUT
        mock_run.return_value = subprocess.CompletedProcess([], 0, "ok", "")
        cmd = ["git", "status"]

        run_cli(cmd, capture_output=True, text=True)

        call_args = mock_run.call_args
        assert call_args[1]["timeout"] == DEFAULT_TIMEOUT

    @patch("app.cli_exec.subprocess.run")
    def test_explicit_timeout_not_overridden(self, mock_run):
        """run_cli respects an explicit timeout from the caller."""
        mock_run.return_value = subprocess.CompletedProcess([], 0, "ok", "")
        cmd = ["git", "status"]

        run_cli(cmd, capture_output=True, text=True, timeout=42)

        call_args = mock_run.call_args
        assert call_args[1]["timeout"] == 42

    @patch("app.provider.get_provider", return_value=CodexProvider())
    @patch("fcntl.flock")
    @patch("app.cli_exec.subprocess.run")
    def test_codex_run_cli_uses_file_lock(self, mock_run, mock_flock, _mock_provider):
        import fcntl

        mock_run.return_value = subprocess.CompletedProcess([], 0, "ok", "")
        cmd = ["codex", "exec", "prompt"]

        run_cli(cmd, capture_output=True, text=True)

        assert mock_flock.call_args_list[0][0][1] == fcntl.LOCK_EX | fcntl.LOCK_NB
        assert mock_flock.call_args_list[-1][0][1] == fcntl.LOCK_UN


class TestProviderLockPath:
    """The provider lock lives under the per-uid koan_tmp_dir (collision fix)."""

    def test_lock_path_under_koan_tmp_dir(self, tmp_path, monkeypatch):
        from app import utils
        from app.cli_exec import _lock_path

        monkeypatch.setattr(utils, "_koan_tmp_dir_cache", None)
        monkeypatch.setenv("KOAN_TMP_DIR", str(tmp_path))

        assert _lock_path("codex-cli") == str(tmp_path / "codex-cli.lock")

    def test_lock_paths_differ_per_user(self, tmp_path, monkeypatch):
        """Different users (tmp roots) get distinct lock paths — no clash."""
        from app import utils
        from app.cli_exec import _lock_path

        monkeypatch.setattr(utils, "_koan_tmp_dir_cache", None)
        monkeypatch.setenv("KOAN_TMP_DIR", str(tmp_path / "user_a"))
        a = _lock_path("codex")

        monkeypatch.setattr(utils, "_koan_tmp_dir_cache", None)
        monkeypatch.setenv("KOAN_TMP_DIR", str(tmp_path / "user_b"))
        b = _lock_path("codex")

        assert a != b
        assert a.endswith("codex.lock")
        assert b.endswith("codex.lock")

    def test_blank_lock_name_defaults_to_provider(self, tmp_path, monkeypatch):
        from app import utils
        from app.cli_exec import _lock_path

        monkeypatch.setattr(utils, "_koan_tmp_dir_cache", None)
        monkeypatch.setenv("KOAN_TMP_DIR", str(tmp_path))

        assert _lock_path("   ") == str(tmp_path / "provider.lock")


# ---------------------------------------------------------------------------
# popen_cli
# ---------------------------------------------------------------------------

class TestPopenCli:

    @patch("app.cli_exec.subprocess.Popen")
    def test_returns_proc_and_cleanup(self, mock_popen):
        mock_proc = MagicMock()
        mock_popen.return_value = mock_proc
        cmd = ["claude", "-p", "secret", "--model", "opus"]

        proc, cleanup = popen_cli(cmd, stdout=subprocess.PIPE)

        assert proc is mock_proc
        actual_cmd = mock_popen.call_args[0][0]
        assert actual_cmd[2] == STDIN_PLACEHOLDER

        # Cleanup should remove the temp file
        import glob
        from app.utils import koan_tmp_dir
        pat = os.path.join(koan_tmp_dir(), "koan-prompt-*")
        before = set(glob.glob(pat))
        cleanup()
        after = set(glob.glob(pat))
        assert len(after) <= len(before)

    @patch("app.cli_exec.subprocess.Popen")
    def test_launcher_is_prefixed_after_the_prompt_rewrite(self, mock_popen):
        """mission_scope wraps the provider CLI without hiding it from the rewrite."""
        mock_popen.return_value = MagicMock()
        cmd = ["claude", "-p", "secret", "--model", "opus"]

        proc, cleanup = popen_cli(
            cmd, launcher=["systemd-run", "--scope", "--"], stdout=subprocess.PIPE,
        )

        actual_cmd = mock_popen.call_args[0][0]
        assert actual_cmd[:3] == ["systemd-run", "--scope", "--"]
        assert actual_cmd[3] == "claude"
        # The prompt was still rewritten to the stdin marker.
        assert STDIN_PLACEHOLDER in actual_cmd
        cleanup()

    @patch("app.cli_exec.subprocess.Popen")
    def test_no_launcher_leaves_argv_untouched(self, mock_popen):
        mock_popen.return_value = MagicMock()
        proc, cleanup = popen_cli(["claude", "-p", "x"], launcher=None)
        assert mock_popen.call_args[0][0][0] == "claude"
        cleanup()

    @patch("app.cli_exec.subprocess.Popen")
    def test_no_p_flag_returns_noop_cleanup(self, mock_popen):
        mock_popen.return_value = MagicMock()
        cmd = ["git", "status"]

        proc, cleanup = popen_cli(cmd)

        call_args = mock_popen.call_args
        assert call_args[1].get("stdin", subprocess.DEVNULL) == subprocess.DEVNULL
        cleanup()  # should not raise

    @patch("app.cli_exec.subprocess.Popen")
    def test_stdin_is_file_object(self, mock_popen):
        mock_popen.return_value = MagicMock()
        cmd = ["claude", "-p", "prompt"]

        proc, cleanup = popen_cli(cmd)

        call_args = mock_popen.call_args
        stdin_arg = call_args[1]["stdin"]
        assert hasattr(stdin_arg, "read")  # it's a file object
        cleanup()

    @patch("app.provider.get_provider", return_value=CopilotProvider())
    @patch("app.cli_exec.subprocess.Popen")
    def test_copilot_keeps_prompt_in_args(self, mock_popen, _mock_provider):
        """Copilot provider: popen keeps prompt in -p, stdin is DEVNULL."""
        mock_popen.return_value = MagicMock()
        cmd = ["copilot", "-p", "my prompt"]

        proc, cleanup = popen_cli(cmd)

        actual_cmd = mock_popen.call_args[0][0]
        assert actual_cmd == ["copilot", "-p", "my prompt"]
        assert mock_popen.call_args[1]["stdin"] == subprocess.DEVNULL
        cleanup()

    @patch("app.provider.get_provider", return_value=CodexProvider())
    @patch("fcntl.flock")
    @patch("app.cli_exec.subprocess.Popen")
    def test_codex_popen_lock_released_on_cleanup(
        self, mock_popen, mock_flock, _mock_provider
    ):
        import fcntl

        mock_popen.return_value = MagicMock()
        cmd = ["codex", "exec", "prompt"]

        _proc, cleanup = popen_cli(cmd)

        assert mock_flock.call_args_list[0][0][1] == fcntl.LOCK_EX | fcntl.LOCK_NB
        cleanup()
        assert mock_flock.call_args_list[-1][0][1] == fcntl.LOCK_UN

    @patch("app.provider.get_provider", return_value=CodexProvider())
    @patch("fcntl.flock")
    def test_codex_popen_releases_lock_when_prompt_open_fails(
        self, mock_flock, _mock_provider
    ):
        """If open(prompt_path) fails after the lock is taken, it must release."""
        import fcntl

        real_open = open

        def fake_open(path, *args, **kwargs):
            # Fail only on the temp prompt file; let the lock file open normally.
            if str(path).endswith(".md"):
                raise OSError("simulated prompt-file open failure")
            return real_open(path, *args, **kwargs)

        cmd = ["codex", "exec", "prompt"]
        with patch("app.cli_exec.open", side_effect=fake_open):
            with pytest.raises(OSError):
                popen_cli(cmd)

        assert mock_flock.call_args_list[0][0][1] == fcntl.LOCK_EX | fcntl.LOCK_NB
        assert mock_flock.call_args_list[-1][0][1] == fcntl.LOCK_UN


class TestProviderInvocationLock:
    """Degraded-state behaviour of _ProviderInvocationLock."""

    def test_no_lock_name_is_noop(self):
        from app.cli_exec import _ProviderInvocationLock

        lock = _ProviderInvocationLock("")
        with lock as entered:
            assert entered is lock
            assert lock.acquired is False
        # release() on an unacquired lock must not raise.
        lock.release()

    @patch("fcntl.flock", side_effect=OSError("flock unsupported"))
    def test_flock_failure_degrades_without_raising(self, _mock_flock):
        from app.cli_exec import _ProviderInvocationLock

        lock = _ProviderInvocationLock("codex-cli")
        # Acquisition failure must degrade loudly, not raise.
        with lock as entered:
            assert entered is lock
            assert lock.acquired is False
        # Behaves as a no-op context; release stays safe.
        lock.release()


# ---------------------------------------------------------------------------
# stream_with_timeout
# ---------------------------------------------------------------------------


class _FakeStream:
    def __init__(self, lines=None, read_text=""):
        self._lines = list(lines or [])
        self._read_text = read_text
        self.closed = False

    def __iter__(self):
        return iter(self._lines)

    def read(self):
        return self._read_text

    def close(self):
        self.closed = True


def _fake_proc(stdout_lines, stderr_text="", returncode=0, pid=99999):
    proc = MagicMock()
    proc.stdout = _FakeStream(lines=stdout_lines)
    proc.stderr = _FakeStream(read_text=stderr_text)
    proc.returncode = returncode
    proc.pid = pid
    proc.wait.return_value = returncode
    return proc


class TestStreamWithTimeout:
    """Tests for stream_with_timeout — shared streaming + watchdog helper."""

    def test_collects_stdout_lines(self):
        proc = _fake_proc(["a\n", "b\n", "c\n"], returncode=0)
        result = stream_with_timeout(proc, timeout=10)
        assert result.stdout == "a\nb\nc"
        assert result.stderr == ""
        assert result.timed_out is False

    def test_returns_full_stdout_not_just_tail(self):
        # 500 lines of output; the returned result must contain all of them,
        # proving the result accumulator is intentionally unbounded (guards
        # against an over-eager future cap — see #2173).
        lines = [f"{i}\n" for i in range(500)]
        proc = _fake_proc(lines, returncode=0)
        result = stream_with_timeout(proc, timeout=30)
        out = result.stdout.splitlines()
        assert out[0] == "0"
        assert out[-1] == "499"
        assert len(out) == 500

    def test_forwards_each_line_to_callback(self):
        proc = _fake_proc(["one\n", "two\n", "three\n"], returncode=0)
        seen = []
        stream_with_timeout(proc, timeout=10, on_line=seen.append)
        assert seen == ["one", "two", "three"]

    def test_drains_stderr(self):
        proc = _fake_proc(["ok\n"], stderr_text="oops", returncode=1)
        result = stream_with_timeout(proc, timeout=10)
        assert result.stderr == "oops"
        assert result.timed_out is False

    def test_closes_streams(self):
        proc = _fake_proc(["ok\n"], returncode=0)
        stream_with_timeout(proc, timeout=10)
        assert proc.stdout.closed is True
        assert proc.stderr.closed is True

    def test_timeout_kills_process_group(self):
        """When the watchdog fires it must SIGKILL the whole process group."""
        import threading

        killed = threading.Event()

        class _BlockingStream:
            def __iter__(self):
                killed.wait(timeout=10)
                return iter([])

            def read(self):
                return ""

            def close(self):
                return None

        proc = MagicMock()
        proc.stdout = _BlockingStream()
        proc.stderr = _FakeStream(read_text="")
        proc.returncode = -9
        proc.pid = 12345
        proc.wait.return_value = -9

        with patch("app.subprocess_runner.os.killpg",
                   side_effect=lambda *a, **kw: killed.set()) as killpg, \
                patch("app.subprocess_runner.os.getpgid", return_value=12345):
            result = stream_with_timeout(proc, timeout=0.5)

        assert result.timed_out is True
        killpg.assert_called_once()

    def test_idle_timeout_sets_timeout_kind(self):
        import threading

        killed = threading.Event()

        class _BlockingStream:
            def __iter__(self):
                killed.wait(timeout=10)
                return iter([])

            def read(self):
                return ""

            def close(self):
                return None

        proc = MagicMock()
        proc.stdout = _BlockingStream()
        proc.stderr = _FakeStream(read_text="")
        proc.returncode = -9
        proc.pid = 12345
        proc.wait.return_value = -9

        with patch("app.subprocess_runner.os.killpg",
                   side_effect=lambda *a, **kw: killed.set()), \
                patch("app.subprocess_runner.os.getpgid", return_value=12345):
            result = stream_with_timeout(
                proc, timeout=10, idle_timeout=0.5, max_duration=20,
            )

        assert result.timed_out is True
        assert result.timeout_kind == "idle"

    def test_max_duration_timeout_sets_timeout_kind(self):
        import threading

        killed = threading.Event()

        class _BlockingStream:
            def __iter__(self):
                killed.wait(timeout=10)
                return iter([])

            def read(self):
                return ""

            def close(self):
                return None

        proc = MagicMock()
        proc.stdout = _BlockingStream()
        proc.stderr = _FakeStream(read_text="")
        proc.returncode = -9
        proc.pid = 12345
        proc.wait.return_value = -9

        with patch("app.subprocess_runner.os.killpg",
                   side_effect=lambda *a, **kw: killed.set()), \
                patch("app.subprocess_runner.os.getpgid", return_value=12345):
            result = stream_with_timeout(proc, timeout=10, max_duration=0.5)

        assert result.timed_out is True
        assert result.timeout_kind == "max_duration"

    def test_completed_flag_blocks_watchdog_race(self):
        """If the watchdog Timer fires after stream EOF but before
        ``watchdog.cancel()``, the kill must be skipped and ``timed_out``
        must stay False — otherwise a clean completion gets reported as
        a timeout."""
        from app.cli_exec import stream_with_timeout as swt

        proc = _fake_proc(["done\n"], returncode=0)

        with patch("app.subprocess_runner.threading.Timer") as TimerMock:
            timer_instance = MagicMock()
            captured = {}

            def factory(timeout, fn):
                captured["fn"] = fn
                return timer_instance

            TimerMock.side_effect = factory

            with patch("app.subprocess_runner.os.killpg") as killpg:
                # Simulate the race: invoke the watchdog callback after
                # stream consumption but before cancel() returns.
                def fire_after_stream():
                    captured["fn"]()
                    return None
                timer_instance.cancel.side_effect = fire_after_stream

                result = swt(proc, timeout=10)

            killpg.assert_not_called()
            assert result.timed_out is False


# ---------------------------------------------------------------------------
# Non-UTF-8 resilience
# ---------------------------------------------------------------------------


class TestNonUtf8Resilience:
    """Subprocess stdout containing invalid UTF-8 must not crash the reader.

    Razor2-Client-Agent contains binary spam test data (0xff bytes).  When
    Claude reads those files, the raw bytes can leak into CLI stdout.
    Previously ``text=True`` without ``errors="replace"`` caused:
      UnicodeDecodeError: 'utf-8' codec can't decode byte 0xff in position 8903
    """

    def test_stream_with_timeout_survives_invalid_utf8(self):
        """Real subprocess emitting 0xff bytes must not crash stream_with_timeout."""
        import sys

        script = (
            "import sys, os; "
            "os.write(1, b'valid line\\n'); "
            "os.write(1, b'bad byte \\xff here\\n'); "
            "os.write(1, b'after bad\\n')"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
        )
        result = stream_with_timeout(proc, timeout=10)
        assert "valid line" in result.stdout
        assert "after bad" in result.stdout
        assert result.timed_out is False

    def test_popen_cli_passes_errors_replace(self):
        """popen_cli must forward errors='replace' to Popen."""
        with patch("app.cli_exec.subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock()
            cmd = ["git", "status"]
            proc, cleanup = popen_cli(
                cmd, stdout=subprocess.PIPE, encoding="utf-8", errors="replace",
            )
            call_kwargs = mock_popen.call_args[1]
            assert call_kwargs["errors"] == "replace"
            cleanup()
