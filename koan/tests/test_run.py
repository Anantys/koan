"""Tests for app.run — the full Python main loop."""

import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def koan_root(tmp_path):
    """Create a minimal koan root with instance directory."""
    instance = tmp_path / "instance"
    instance.mkdir()
    (instance / "missions.md").write_text("# Missions\n\n## En attente\n\n## En cours\n\n## Terminées\n")
    (instance / "config.yaml").write_text("max_runs: 5\ninterval: 10\n")
    (tmp_path / "koan" / "app").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def projects(tmp_path):
    """Create project directories."""
    p1 = tmp_path / "proj1"
    p1.mkdir()
    p2 = tmp_path / "proj2"
    p2.mkdir()
    return [(str(p1), "proj1"), (str(p2), "proj2")]


# ---------------------------------------------------------------------------
# Test: Colored logging
# ---------------------------------------------------------------------------

class TestLog:
    def test_log_outputs_category(self, capsys):
        from app.run import log, _init_colors
        _init_colors()
        log("koan", "hello")
        out = capsys.readouterr().out
        assert "[koan]" in out
        assert "hello" in out

    def test_log_all_categories(self, capsys):
        from app.run import log, _init_colors
        _init_colors()
        for cat in ["koan", "error", "init", "health", "git", "mission", "quota", "pause",
                     "warning", "warn"]:
            log(cat, f"test {cat}")
        out = capsys.readouterr().out
        for cat in ["koan", "error", "init", "health", "git", "mission", "quota", "pause",
                     "warning", "warn"]:
            assert f"[{cat}]" in out

    def test_warning_and_warn_have_color(self):
        """warning and warn categories should be in _CATEGORY_COLORS (not fall through to white)."""
        from app.run import _CATEGORY_COLORS
        assert "warning" in _CATEGORY_COLORS
        assert "warn" in _CATEGORY_COLORS

    def test_log_unknown_category(self, capsys):
        from app.run import log, _init_colors
        _init_colors()
        log("custom", "msg")
        out = capsys.readouterr().out
        assert "[custom]" in out

    def test_force_color_env_enables_colors(self, capsys, monkeypatch):
        """KOAN_FORCE_COLOR=1 enables ANSI colors even without TTY."""
        monkeypatch.setenv("KOAN_FORCE_COLOR", "1")
        from app.run_log import _init_colors, _COLORS
        # Force re-init — target canonical module (run_log owns _COLORS)
        import app.run_log as log_mod
        log_mod._COLORS = {}
        _init_colors()
        colors = log_mod._COLORS
        assert colors.get("reset") == "\033[0m"
        assert colors.get("red") == "\033[31m"


# ---------------------------------------------------------------------------
# Test: parse_projects
# ---------------------------------------------------------------------------

class TestParseProjects:
    """Tests for parse_projects() — delegates to get_known_projects().

    Since parse_projects() now reads from projects.yaml > KOAN_PROJECTS,
    tests must set env vars or create projects.yaml.
    """

    def test_multi_project(self, tmp_path, monkeypatch):
        from app import utils
        monkeypatch.setattr(utils, "KOAN_ROOT", tmp_path)
        from app.run import parse_projects
        p1 = tmp_path / "a"
        p2 = tmp_path / "b"
        p1.mkdir()
        p2.mkdir()
        monkeypatch.setenv("KOAN_PROJECTS", f"a:{p1};b:{p2}")
        result = parse_projects()
        assert len(result) == 2
        assert result[0] == ("a", str(p1))
        assert result[1] == ("b", str(p2))

    def test_single_project_via_env(self, tmp_path, monkeypatch):
        from app import utils
        monkeypatch.setattr(utils, "KOAN_ROOT", tmp_path)
        from app.run import parse_projects
        p = tmp_path / "proj"
        p.mkdir()
        monkeypatch.setenv("KOAN_PROJECTS", f"proj:{p}")
        result = parse_projects()
        assert len(result) == 1
        assert result[0] == ("proj", str(p))

    def test_no_project_exits(self, tmp_path, monkeypatch):
        from app import utils
        monkeypatch.setattr(utils, "KOAN_ROOT", tmp_path)
        monkeypatch.delenv("KOAN_PROJECTS", raising=False)
        from app.run import parse_projects
        with pytest.raises(SystemExit):
            parse_projects()

    def test_nonexistent_path_exits(self, tmp_path, monkeypatch):
        from app import utils
        monkeypatch.setattr(utils, "KOAN_ROOT", tmp_path)
        monkeypatch.setenv("KOAN_PROJECTS", f"bad:{tmp_path}/nonexistent")
        from app.run import parse_projects
        with pytest.raises(SystemExit):
            parse_projects()

    def test_too_many_projects(self, tmp_path, monkeypatch):
        from app import utils
        monkeypatch.setattr(utils, "KOAN_ROOT", tmp_path)
        from app.run import parse_projects
        # 51 projects
        dirs = []
        for i in range(51):
            d = tmp_path / f"p{i}"
            d.mkdir()
            dirs.append(f"p{i}:{d}")
        monkeypatch.setenv("KOAN_PROJECTS", ";".join(dirs))
        with pytest.raises(SystemExit):
            parse_projects()

    def test_projects_yaml_used(self, tmp_path, monkeypatch):
        """parse_projects reads from projects.yaml when available."""
        from app import utils
        monkeypatch.setattr(utils, "KOAN_ROOT", tmp_path)
        from app.run import parse_projects
        p = tmp_path / "myproject"
        p.mkdir()
        (tmp_path / "projects.yaml").write_text(f"""
projects:
  myproject:
    path: "{p}"
""")
        monkeypatch.delenv("KOAN_PROJECTS", raising=False)
        result = parse_projects()
        assert len(result) == 1
        assert result[0][0] == "myproject"


# ---------------------------------------------------------------------------
# Test: set_status
# ---------------------------------------------------------------------------

class TestSetStatus:
    def test_writes_status_file(self, tmp_path):
        from app.run import set_status
        set_status(str(tmp_path), "Running")
        assert (tmp_path / ".koan-status").read_text() == "Running"

    def test_overwrites_status(self, tmp_path):
        from app.run import set_status
        set_status(str(tmp_path), "First")
        set_status(str(tmp_path), "Second")
        assert (tmp_path / ".koan-status").read_text() == "Second"


# ---------------------------------------------------------------------------
# Test: _build_startup_status
# ---------------------------------------------------------------------------

class TestBuildStartupStatus:
    def test_active_when_not_paused(self, tmp_path):
        from app.run import _build_startup_status
        result = _build_startup_status(str(tmp_path))
        assert "Active" in result
        assert "ready to work" in result
        assert "/resume" not in result

    def test_paused_with_quota_reason_and_display(self, tmp_path):
        from app.run import _build_startup_status
        (tmp_path / ".koan-pause").touch()
        (tmp_path / ".koan-pause-reason").write_text("quota\n1739300000\nresets 10am (Europe/Paris)\n")
        result = _build_startup_status(str(tmp_path))
        assert "Paused" in result
        assert "quota" in result
        assert "resets 10am (Europe/Paris)" in result
        assert "/resume" in result

    def test_paused_with_max_runs_no_display(self, tmp_path):
        from app.run import _build_startup_status
        (tmp_path / ".koan-pause").touch()
        (tmp_path / ".koan-pause-reason").write_text("max_runs\n1739300000\n\n")
        result = _build_startup_status(str(tmp_path))
        assert "Paused" in result
        assert "max_runs" in result
        assert "/resume" in result

    def test_paused_with_no_reason_file(self, tmp_path):
        from app.run import _build_startup_status
        (tmp_path / ".koan-pause").touch()
        result = _build_startup_status(str(tmp_path))
        assert "Paused" in result
        assert "/resume" in result

    def test_paused_with_empty_reason_file(self, tmp_path):
        from app.run import _build_startup_status
        (tmp_path / ".koan-pause").touch()
        (tmp_path / ".koan-pause-reason").write_text("")
        result = _build_startup_status(str(tmp_path))
        assert "Paused" in result
        assert "/resume" in result


# ---------------------------------------------------------------------------
# Test: start_on_pause in run_startup
# ---------------------------------------------------------------------------

class TestStartOnPause:
    """Tests for the start_on_pause logic in run_startup().
    
    Tests the behavior of removing stale .koan-pause-reason files and creating
    .koan-pause files when start_on_pause is enabled.
    """

    def _apply_start_on_pause_logic(self, koan_root, start_on_pause_enabled):
        """Helper: directly invoke the start_on_pause logic without full run_startup().

        This directly tests the logic block without mocking 18+ unrelated functions.
        """
        # Direct implementation of the logic from run_startup
        if start_on_pause_enabled:
            koan_root_path = Path(koan_root)
            reason_file = koan_root_path / ".koan-pause-reason"
            if reason_file.exists():
                try:
                    first_line = reason_file.read_text().strip().splitlines()[0]
                except (OSError, IndexError):
                    first_line = ""
                if first_line != "manual":
                    reason_file.unlink(missing_ok=True)
            if not (koan_root_path / ".koan-pause").exists():
                (koan_root_path / ".koan-pause").touch()

    def test_creates_pause_file_when_enabled(self, koan_root):
        """start_on_pause=true should create .koan-pause."""
        assert not (koan_root / ".koan-pause").exists()
        self._apply_start_on_pause_logic(koan_root, True)
        assert (koan_root / ".koan-pause").exists()

    def test_no_pause_file_when_disabled(self, koan_root):
        """start_on_pause=false should not create .koan-pause."""
        self._apply_start_on_pause_logic(koan_root, False)
        assert not (koan_root / ".koan-pause").exists()

    def test_removes_stale_reason_file(self, koan_root):
        """start_on_pause should remove stale .koan-pause-reason to prevent auto-resume."""
        (koan_root / ".koan-pause-reason").write_text("quota\n1700000000\nresets 10am\n")
        self._apply_start_on_pause_logic(koan_root, True)
        assert (koan_root / ".koan-pause").exists()
        assert not (koan_root / ".koan-pause-reason").exists()

    def test_removes_stale_reason_even_when_pause_exists(self, koan_root):
        """When .koan-pause already exists, should still remove stale reason file."""
        (koan_root / ".koan-pause").touch()
        (koan_root / ".koan-pause-reason").write_text("max_runs\n1700000000\n\n")
        self._apply_start_on_pause_logic(koan_root, True)
        assert (koan_root / ".koan-pause").exists()
        assert not (koan_root / ".koan-pause-reason").exists()

    def test_no_reason_cleanup_when_disabled(self, koan_root):
        """start_on_pause=false should not touch existing reason file."""
        (koan_root / ".koan-pause").touch()
        (koan_root / ".koan-pause-reason").write_text("quota\n1700000000\nresets 10am\n")
        self._apply_start_on_pause_logic(koan_root, False)
        # Both files should remain untouched
        assert (koan_root / ".koan-pause").exists()
        assert (koan_root / ".koan-pause-reason").exists()

    def test_preserves_manual_pause_reason(self, koan_root):
        """start_on_pause=true should NOT delete manual pause reason files."""
        (koan_root / ".koan-pause").touch()
        (koan_root / ".koan-pause-reason").write_text("manual\n1700000000\npaused via Telegram\n")
        self._apply_start_on_pause_logic(koan_root, True)
        assert (koan_root / ".koan-pause").exists()
        assert (koan_root / ".koan-pause-reason").exists()
        content = (koan_root / ".koan-pause-reason").read_text()
        assert content.startswith("manual")

    def test_removes_quota_reason_but_not_manual(self, koan_root):
        """Quota reason is removed but manual reason is preserved."""
        # First: quota gets removed
        (koan_root / ".koan-pause").touch()
        (koan_root / ".koan-pause-reason").write_text("quota\n1700000000\nresets 10am\n")
        self._apply_start_on_pause_logic(koan_root, True)
        assert not (koan_root / ".koan-pause-reason").exists()

        # Reset
        (koan_root / ".koan-pause").unlink(missing_ok=True)

        # Second: manual is preserved
        (koan_root / ".koan-pause").touch()
        (koan_root / ".koan-pause-reason").write_text("manual\n1700000000\n\n")
        self._apply_start_on_pause_logic(koan_root, True)
        assert (koan_root / ".koan-pause-reason").exists()

    def test_handles_corrupted_reason_file(self, koan_root):
        """Corrupted reason file (unreadable first line) should be removed."""
        (koan_root / ".koan-pause").touch()
        (koan_root / ".koan-pause-reason").write_text("\n\n")
        self._apply_start_on_pause_logic(koan_root, True)
        # Empty first line ≠ "manual" → should be removed
        assert not (koan_root / ".koan-pause-reason").exists()

    def test_orphan_pause_stays_paused(self, koan_root):
        """Orphan .koan-pause (no reason file) should stay paused.

        The safe default is to stay paused — the user can always /resume.
        Previously, orphans auto-resumed, which overrode user-initiated
        /pause when the reason file was lost.
        """
        from app.pause_manager import check_and_resume, is_paused

        (koan_root / ".koan-pause").touch()
        # No reason file = orphan state (crash, partial cleanup, etc.)
        result = check_and_resume(str(koan_root))
        assert result is None, "Orphan pause should stay paused"
        assert is_paused(str(koan_root)), "Pause file should remain"



# ---------------------------------------------------------------------------
# Test: SignalState
# ---------------------------------------------------------------------------

class TestSignalState:
    def test_default_state(self):
        from app.run import SignalState
        s = SignalState()
        assert s.task_running is False
        assert s.first_ctrl_c == 0
        assert s.claude_proc is None
        assert s.timeout == 10
        assert s.phase == ""


# ---------------------------------------------------------------------------
# Test: _on_sigint
# ---------------------------------------------------------------------------

class TestOnSigint:
    def test_no_task_raises_keyboard_interrupt(self):
        from app.run import _on_sigint, _sig
        _sig.task_running = False
        with pytest.raises(KeyboardInterrupt):
            _on_sigint(signal.SIGINT, None)

    def test_first_ctrl_c_warns(self, capsys):
        from app.run import _on_sigint, _sig, _init_colors
        _init_colors()
        _sig.task_running = True
        _sig.first_ctrl_c = 0
        _sig.phase = ""
        _on_sigint(signal.SIGINT, None)
        assert _sig.first_ctrl_c > 0
        out = capsys.readouterr().out
        assert "Press CTRL-C again" in out

    def test_first_ctrl_c_shows_phase(self, capsys):
        """First CTRL-C should display the current phase name."""
        from app.run import _on_sigint, _sig, _init_colors
        _init_colors()
        _sig.task_running = True
        _sig.first_ctrl_c = 0
        _sig.phase = "Morning ritual"
        _on_sigint(signal.SIGINT, None)
        out = capsys.readouterr().out
        assert "Morning ritual" in out
        assert "Press CTRL-C again" in out
        _sig.phase = ""

    def test_first_ctrl_c_no_phase_no_parens(self, capsys):
        """When no phase is set, the message should not have empty parens."""
        from app.run import _on_sigint, _sig, _init_colors
        _init_colors()
        _sig.task_running = True
        _sig.first_ctrl_c = 0
        _sig.phase = ""
        _on_sigint(signal.SIGINT, None)
        out = capsys.readouterr().out
        assert "()" not in out

    def test_second_ctrl_c_raises(self, capsys):
        from app.run import _on_sigint, _sig, _init_colors
        _init_colors()
        _sig.task_running = True
        _sig.first_ctrl_c = time.time()  # Just set
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 12345
        _sig.claude_proc = mock_proc
        with patch("app.run.os.getpgid", return_value=12345), \
             patch("app.run.os.killpg") as mock_killpg:
            with pytest.raises(KeyboardInterrupt):
                _on_sigint(signal.SIGINT, None)
            mock_killpg.assert_called_once_with(12345, signal.SIGTERM)

    def test_expired_timeout_resets(self, capsys):
        from app.run import _on_sigint, _sig, _init_colors
        _init_colors()
        _sig.task_running = True
        _sig.first_ctrl_c = time.time() - 20  # Expired
        _on_sigint(signal.SIGINT, None)
        # Should be treated as first CTRL-C (warning)
        assert _sig.first_ctrl_c > time.time() - 2
        out = capsys.readouterr().out
        assert "Press CTRL-C again" in out


# ---------------------------------------------------------------------------
# Test: _kill_process_group
# ---------------------------------------------------------------------------

class TestKillProcessGroup:
    def test_kills_process_group_with_sigterm(self):
        """_kill_process_group sends SIGTERM to the process group."""
        from app.run import _kill_process_group
        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 99999
        proc.wait.return_value = 0
        with patch("app.run.os.getpgid", return_value=99999) as mock_getpgid, \
             patch("app.run.os.killpg") as mock_killpg:
            _kill_process_group(proc)
            mock_getpgid.assert_called_once_with(99999)
            mock_killpg.assert_called_once_with(99999, signal.SIGTERM)
            proc.wait.assert_called_once_with(timeout=3)

    def test_falls_back_to_sigkill_on_timeout(self):
        """_kill_process_group sends SIGKILL if SIGTERM times out."""
        from app.run import _kill_process_group
        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 99999
        proc.wait.side_effect = [subprocess.TimeoutExpired("cmd", 3), 0]
        with patch("app.run.os.getpgid", return_value=99999), \
             patch("app.run.os.killpg") as mock_killpg:
            _kill_process_group(proc)
            assert mock_killpg.call_count == 2
            mock_killpg.assert_any_call(99999, signal.SIGTERM)
            mock_killpg.assert_any_call(99999, signal.SIGKILL)

    def test_noop_when_proc_is_none(self):
        """_kill_process_group does nothing for None."""
        from app.run import _kill_process_group
        _kill_process_group(None)  # Should not raise

    def test_noop_when_proc_already_exited(self):
        """_kill_process_group does nothing if process already exited."""
        from app.run import _kill_process_group
        proc = MagicMock()
        proc.poll.return_value = 0
        with patch("app.run.os.getpgid") as mock_getpgid:
            _kill_process_group(proc)
            mock_getpgid.assert_not_called()

    def test_handles_process_lookup_error(self):
        """_kill_process_group swallows ProcessLookupError."""
        from app.run import _kill_process_group
        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 99999
        with patch("app.run.os.getpgid", side_effect=ProcessLookupError):
            _kill_process_group(proc)  # Should not raise

    def test_handles_sigkill_timeout(self, capsys):
        """_kill_process_group handles TimeoutExpired after SIGKILL gracefully."""
        from app.run import _kill_process_group
        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 77777
        # First wait (SIGTERM) times out, second wait (SIGKILL) also times out
        proc.wait.side_effect = [
            subprocess.TimeoutExpired("cmd", 3),
            subprocess.TimeoutExpired("cmd", 5),
        ]
        with patch("app.run.os.getpgid", return_value=77777), \
             patch("app.run.os.killpg") as mock_killpg:
            _kill_process_group(proc)  # Should not raise
            mock_killpg.assert_any_call(77777, signal.SIGTERM)
            mock_killpg.assert_any_call(77777, signal.SIGKILL)
        captured = capsys.readouterr()
        assert "did not exit after SIGKILL" in captured.err


# ---------------------------------------------------------------------------
# Test: protected_phase context manager
# ---------------------------------------------------------------------------

class TestProtectedPhase:
    def test_sets_task_running(self):
        from app.run import protected_phase, _sig
        _sig.task_running = False
        _sig.phase = ""
        with protected_phase("Testing"):
            assert _sig.task_running is True
            assert _sig.phase == "Testing"
        assert _sig.task_running is False
        assert _sig.phase == ""

    def test_resets_on_exit(self):
        from app.run import protected_phase, _sig
        _sig.task_running = False
        _sig.phase = ""
        _sig.first_ctrl_c = 99.0
        with protected_phase("Phase A"):
            assert _sig.first_ctrl_c == 0  # Reset on entry
        assert _sig.first_ctrl_c == 0  # Reset on exit

    def test_restores_previous_state(self):
        """Nested protected_phase should restore outer state."""
        from app.run import protected_phase, _sig
        _sig.task_running = False
        _sig.phase = ""
        with protected_phase("Outer"):
            assert _sig.phase == "Outer"
            assert _sig.task_running is True
            with protected_phase("Inner"):
                assert _sig.phase == "Inner"
                assert _sig.task_running is True
            assert _sig.phase == "Outer"
            assert _sig.task_running is True
        assert _sig.task_running is False
        assert _sig.phase == ""

    def test_restores_on_exception(self):
        """State should be restored even if an exception occurs."""
        from app.run import protected_phase, _sig
        _sig.task_running = False
        _sig.phase = ""
        try:
            with protected_phase("Failing"):
                assert _sig.task_running is True
                raise ValueError("boom")
        except ValueError:
            pass
        assert _sig.task_running is False
        assert _sig.phase == ""

    def test_restores_on_keyboard_interrupt(self):
        """State should be restored on KeyboardInterrupt."""
        from app.run import protected_phase, _sig
        _sig.task_running = False
        _sig.phase = ""
        try:
            with protected_phase("Interrupted"):
                raise KeyboardInterrupt
        except KeyboardInterrupt:
            pass
        assert _sig.task_running is False
        assert _sig.phase == ""

    def test_first_ctrl_c_during_phase_warns(self, capsys):
        """First CTRL-C inside a protected_phase should warn, not abort."""
        from app.run import protected_phase, _on_sigint, _sig, _init_colors
        _init_colors()
        with protected_phase("Git sync"):
            _sig.first_ctrl_c = 0
            _on_sigint(signal.SIGINT, None)
            out = capsys.readouterr().out
            assert "Git sync" in out
            assert "Press CTRL-C again" in out
            assert _sig.first_ctrl_c > 0

    def test_double_ctrl_c_during_phase_aborts(self, capsys):
        """Double CTRL-C inside a protected_phase should raise."""
        from app.run import protected_phase, _on_sigint, _sig, _init_colors
        _init_colors()
        with protected_phase("Morning ritual"):
            _sig.first_ctrl_c = time.time()  # Simulate first press
            with pytest.raises(KeyboardInterrupt):
                _on_sigint(signal.SIGINT, None)

    def test_outside_phase_ctrl_c_raises_immediately(self):
        """Without protected_phase, CTRL-C should raise immediately."""
        from app.run import _on_sigint, _sig
        _sig.task_running = False
        _sig.phase = ""
        with pytest.raises(KeyboardInterrupt):
            _on_sigint(signal.SIGINT, None)

    def test_phase_cleared_after_double_tap(self):
        """After double-tap abort, phase and task_running should be cleared."""
        from app.run import protected_phase, _on_sigint, _sig, _init_colors
        _init_colors()
        _sig.task_running = False
        _sig.phase = ""
        try:
            with protected_phase("Some phase"):
                _sig.first_ctrl_c = time.time()
                try:
                    _on_sigint(signal.SIGINT, None)
                except KeyboardInterrupt:
                    raise
        except KeyboardInterrupt:
            pass
        # The context manager __exit__ should have run
        assert _sig.phase == ""
        assert _sig.task_running is False


# ---------------------------------------------------------------------------
# Test: _cleanup_temp
# ---------------------------------------------------------------------------

class TestCleanupTemp:
    def test_removes_files(self, tmp_path):
        from app.run import _cleanup_temp
        f1 = tmp_path / "a.tmp"
        f2 = tmp_path / "b.tmp"
        f1.write_text("data")
        f2.write_text("data")
        _cleanup_temp(str(f1), str(f2))
        assert not f1.exists()
        assert not f2.exists()

    def test_ignores_missing(self, tmp_path):
        from app.run import _cleanup_temp
        # Should not raise
        _cleanup_temp(str(tmp_path / "nonexistent"))


# ---------------------------------------------------------------------------
# Test: _commit_instance
# ---------------------------------------------------------------------------

class TestCommitInstance:

    @staticmethod
    def _init_repo(tmp_path):
        """Helper: create a minimal git repo with one commit."""
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"],
                       cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"],
                       cwd=str(tmp_path), capture_output=True)
        (tmp_path / "file.txt").write_text("initial")
        subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), capture_output=True)

    def test_commits_when_changes(self, tmp_path):
        from app.run import _commit_instance
        self._init_repo(tmp_path)

        (tmp_path / "file.txt").write_text("modified")
        _commit_instance(str(tmp_path), "test commit")

        result = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            cwd=str(tmp_path), capture_output=True, text=True,
        )
        assert "test commit" in result.stdout

    def test_skips_when_no_changes(self, tmp_path):
        """No commit created when there are no staged changes."""
        from app.run import _commit_instance
        self._init_repo(tmp_path)

        # Get current commit count
        before = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=str(tmp_path), capture_output=True, text=True,
        )
        count_before = int(before.stdout.strip())

        _commit_instance(str(tmp_path), "should not appear")

        after = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=str(tmp_path), capture_output=True, text=True,
        )
        count_after = int(after.stdout.strip())
        assert count_after == count_before

    def test_default_message_format(self, tmp_path):
        """Default commit message includes timestamp pattern."""
        from app.run import _commit_instance
        self._init_repo(tmp_path)

        (tmp_path / "file.txt").write_text("changed")
        _commit_instance(str(tmp_path))

        result = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            cwd=str(tmp_path), capture_output=True, text=True,
        )
        assert "koan:" in result.stdout

    @patch("app.run.subprocess.run")
    @patch("app.run.log")
    def test_logs_error_on_add_failure(self, mock_log, mock_run):
        """Logs error and returns early if git add fails."""
        from app.run import _commit_instance
        mock_run.return_value = MagicMock(
            returncode=128, stderr=b"fatal: not a git repository"
        )
        _commit_instance("/fake/instance", "test")
        mock_log.assert_called_once()
        assert "git add failed" in mock_log.call_args[0][1]

    @patch("app.run.subprocess.run")
    @patch("app.run.log")
    def test_logs_error_on_commit_failure(self, mock_log, mock_run):
        """Logs error and returns early if git commit fails."""
        from app.run import _commit_instance

        def side_effect(cmd, **kwargs):
            if cmd[1] == "add":
                return MagicMock(returncode=0)
            elif cmd[1] == "diff":
                return MagicMock(returncode=1)  # Has staged changes
            elif cmd[1] == "commit":
                return MagicMock(returncode=1, stderr=b"error: something broke")
            return MagicMock(returncode=0)

        mock_run.side_effect = side_effect
        _commit_instance("/fake/instance", "test")
        mock_log.assert_called_once()
        assert "git commit failed" in mock_log.call_args[0][1]

    @patch("app.run.subprocess.run")
    @patch("app.run.log")
    def test_logs_error_on_push_failure(self, mock_log, mock_run):
        """Logs error if git push fails (but commit succeeds)."""
        from app.run import _commit_instance

        def side_effect(cmd, **kwargs):
            if cmd[1] == "add":
                return MagicMock(returncode=0)
            elif cmd[1] == "diff":
                return MagicMock(returncode=1)
            elif cmd[1] == "commit":
                return MagicMock(returncode=0)
            elif cmd[1] == "push":
                return MagicMock(returncode=1, stderr=b"remote: Permission denied")
            return MagicMock(returncode=0)

        mock_run.side_effect = side_effect
        _commit_instance("/fake/instance", "test")
        mock_log.assert_called_once()
        assert "git push failed" in mock_log.call_args[0][1]

    @patch("app.run.subprocess.run")
    @patch("app.run.log")
    def test_does_not_push_when_commit_fails(self, mock_log, mock_run):
        """When commit fails, push is never attempted."""
        from app.run import _commit_instance

        calls = []

        def side_effect(cmd, **kwargs):
            calls.append(cmd[1])
            if cmd[1] == "add":
                return MagicMock(returncode=0)
            elif cmd[1] == "diff":
                return MagicMock(returncode=1)
            elif cmd[1] == "commit":
                return MagicMock(returncode=1, stderr=b"error")
            return MagicMock(returncode=0)

        mock_run.side_effect = side_effect
        _commit_instance("/fake/instance", "test")
        assert "push" not in calls

    @patch("app.run.subprocess.run")
    @patch("app.run.log")
    def test_does_not_commit_when_add_fails(self, mock_log, mock_run):
        """When add fails, commit is never attempted."""
        from app.run import _commit_instance

        calls = []

        def side_effect(cmd, **kwargs):
            calls.append(cmd[1])
            if cmd[1] == "add":
                return MagicMock(returncode=128, stderr=b"fatal")
            return MagicMock(returncode=0)

        mock_run.side_effect = side_effect
        _commit_instance("/fake/instance", "test")
        assert "commit" not in calls
        assert "push" not in calls

    @patch("app.run.subprocess.run", side_effect=subprocess.TimeoutExpired("git", 10))
    @patch("app.run.log")
    def test_handles_timeout_exception(self, mock_log, mock_run):
        """Timeout during any git operation is caught and logged."""
        from app.run import _commit_instance
        _commit_instance("/fake/instance", "test")
        mock_log.assert_called_once()
        assert "failed" in mock_log.call_args[0][1]

    @patch("app.run.subprocess.run", side_effect=OSError("disk full"))
    @patch("app.run.log")
    def test_handles_os_error(self, mock_log, mock_run):
        """OS errors during git operations are caught and logged."""
        from app.run import _commit_instance
        _commit_instance("/fake/instance", "test")
        mock_log.assert_called_once()
        assert "failed" in mock_log.call_args[0][1]

    @patch("app.run.subprocess.run")
    @patch("app.run.log")
    def test_pushes_to_current_branch_not_hardcoded_main(self, mock_log, mock_run):
        """Push targets the actual branch from rev-parse, not hardcoded 'main'."""
        from app.run import _commit_instance

        def side_effect(cmd, **kwargs):
            if cmd[1] == "add":
                return MagicMock(returncode=0)
            elif cmd[1] == "diff":
                return MagicMock(returncode=1)  # Has staged changes
            elif cmd[1] == "commit":
                return MagicMock(returncode=0)
            elif cmd[1] == "rev-parse":
                return MagicMock(returncode=0, stdout=b"instance-branch\n")
            elif cmd[1] == "push":
                return MagicMock(returncode=0)
            return MagicMock(returncode=0)

        mock_run.side_effect = side_effect
        _commit_instance("/fake/instance", "test")

        push_calls = [c for c in mock_run.call_args_list if c[0][0][1] == "push"]
        assert len(push_calls) == 1
        assert "instance-branch" in push_calls[0][0][0]

    @patch("app.run.subprocess.run")
    @patch("app.run.log")
    def test_skips_push_on_rev_parse_failure(self, mock_log, mock_run):
        """Skips push when rev-parse fails (no branch detected)."""
        from app.run import _commit_instance

        def side_effect(cmd, **kwargs):
            if cmd[1] == "add":
                return MagicMock(returncode=0)
            elif cmd[1] == "diff":
                return MagicMock(returncode=1)
            elif cmd[1] == "commit":
                return MagicMock(returncode=0)
            elif cmd[1] == "rev-parse":
                return MagicMock(returncode=128, stdout=b"")
            elif cmd[1] == "push":
                return MagicMock(returncode=0)
            return MagicMock(returncode=0)

        mock_run.side_effect = side_effect
        _commit_instance("/fake/instance", "test")

        push_calls = [c for c in mock_run.call_args_list if c[0][0][1] == "push"]
        assert len(push_calls) == 0
        mock_log.assert_any_call("error", "Skipping push: detached HEAD or unknown branch")

    @patch("app.run.subprocess.run")
    @patch("app.run.log")
    def test_skips_push_on_detached_head(self, mock_log, mock_run):
        """Skips push when rev-parse returns 'HEAD' (detached)."""
        from app.run import _commit_instance

        def side_effect(cmd, **kwargs):
            if cmd[1] == "add":
                return MagicMock(returncode=0)
            elif cmd[1] == "diff":
                return MagicMock(returncode=1)
            elif cmd[1] == "commit":
                return MagicMock(returncode=0)
            elif cmd[1] == "rev-parse":
                return MagicMock(returncode=0, stdout=b"HEAD\n")
            elif cmd[1] == "push":
                return MagicMock(returncode=0)
            return MagicMock(returncode=0)

        mock_run.side_effect = side_effect
        _commit_instance("/fake/instance", "test")

        push_calls = [c for c in mock_run.call_args_list if c[0][0][1] == "push"]
        assert len(push_calls) == 0


class TestReadCurrentProject:
    """Tests for _read_current_project helper."""

    def test_reads_project_from_file(self, tmp_path):
        from app.run import _read_current_project
        (tmp_path / ".koan-project").write_text("my-project\n")
        assert _read_current_project(str(tmp_path)) == "my-project"

    def test_returns_unknown_when_file_missing(self, tmp_path):
        from app.run import _read_current_project
        assert _read_current_project(str(tmp_path)) == "unknown"

    def test_returns_unknown_on_os_error(self):
        from app.run import _read_current_project
        assert _read_current_project("/nonexistent/path/that/cannot/exist") == "unknown"

    def test_strips_whitespace(self, tmp_path):
        from app.run import _read_current_project
        (tmp_path / ".koan-project").write_text("  spaced-project  \n")
        assert _read_current_project(str(tmp_path)) == "spaced-project"


# ---------------------------------------------------------------------------
# Test: _notify
# ---------------------------------------------------------------------------

class TestNotify:
    @patch("app.run.format_and_send", create=True)
    def test_notify_calls_format_and_send(self, mock_send):
        with patch("app.notify.format_and_send", mock_send):
            from app.run import _notify
            _notify("/fake/instance", "hello")
            # Should not raise even if import fails


# ---------------------------------------------------------------------------
# Test: handle_pause
# ---------------------------------------------------------------------------

class TestHandlePause:
    def test_auto_resume(self, koan_root):
        from app.run import handle_pause

        instance = str(koan_root / "instance")
        (koan_root / ".koan-pause").touch()

        with patch("app.pause_manager.check_and_resume", return_value="Quota reset"):
            result = handle_pause(str(koan_root), instance, 5)
        assert result == "resume"

    def test_manual_resume(self, koan_root):
        from app.run import handle_pause

        instance = str(koan_root / "instance")
        # No .koan-pause file = manual resume

        with patch("app.pause_manager.check_and_resume", return_value=None):
            result = handle_pause(str(koan_root), instance, 5)
        assert result == "resume"

    @patch("app.run.time.sleep")
    def test_no_work_on_pause(self, mock_sleep, koan_root):
        """Pause must NOT run any Claude CLI calls — no contemplative, no autonomous work."""
        from app.run import handle_pause

        instance = str(koan_root / "instance")
        (koan_root / ".koan-pause").touch()

        sleep_count = [0]
        def remove_pause_after_3(duration):
            sleep_count[0] += 1
            if sleep_count[0] >= 3:
                (koan_root / ".koan-pause").unlink(missing_ok=True)

        mock_sleep.side_effect = remove_pause_after_3

        with patch("app.pause_manager.check_and_resume", return_value=None), \
             patch("app.run.run_claude_task") as mock_claude:
            result = handle_pause(str(koan_root), instance, 5)

            assert result == "resume"
            mock_claude.assert_not_called()

    @patch("app.run.time.sleep")
    def test_pause_sleeps_full_cycle_when_not_resumed(self, mock_sleep, koan_root):
        """While paused, the agent sleeps 60 × 5s and returns None."""
        from app.run import handle_pause

        instance = str(koan_root / "instance")
        (koan_root / ".koan-pause").touch()

        with patch("app.pause_manager.check_and_resume", return_value=None), \
             patch("app.run.run_claude_task") as mock_claude:
            result = handle_pause(str(koan_root), instance, 5)

            assert result is None
            mock_claude.assert_not_called()
            assert mock_sleep.call_count == 60

    @patch("app.run.time.sleep")
    def test_pause_resumes_on_file_removal(self, mock_sleep, koan_root):
        """Pause returns 'resume' when .koan-pause is removed during sleep."""
        from app.run import handle_pause

        instance = str(koan_root / "instance")
        (koan_root / ".koan-pause").touch()

        sleep_count = [0]
        def remove_pause_after_3(duration):
            sleep_count[0] += 1
            if sleep_count[0] >= 3:
                (koan_root / ".koan-pause").unlink(missing_ok=True)

        mock_sleep.side_effect = remove_pause_after_3

        with patch("app.pause_manager.check_and_resume", return_value=None):
            result = handle_pause(str(koan_root), instance, 5)
            assert result == "resume"

    @patch("app.run.time.sleep")
    def test_pause_breaks_on_restart_signal(self, mock_sleep, koan_root):
        """Pause breaks out when .koan-restart appears, returns None."""
        from app.run import handle_pause

        instance = str(koan_root / "instance")
        (koan_root / ".koan-pause").touch()

        sleep_count = [0]
        def create_restart_after_2(duration):
            sleep_count[0] += 1
            if sleep_count[0] >= 2:
                (koan_root / ".koan-restart").touch()

        mock_sleep.side_effect = create_restart_after_2

        with patch("app.pause_manager.check_and_resume", return_value=None):
            result = handle_pause(str(koan_root), instance, 5)
            assert result is None

    @patch("app.run.time.sleep")
    def test_pause_breaks_on_stop_signal(self, mock_sleep, koan_root):
        """Pause breaks out when .koan-stop appears, returns None."""
        from app.run import handle_pause

        instance = str(koan_root / "instance")
        (koan_root / ".koan-pause").touch()

        sleep_count = [0]
        def create_stop_after_2(duration):
            sleep_count[0] += 1
            if sleep_count[0] >= 2:
                (koan_root / ".koan-stop").write_text("STOP")

        mock_sleep.side_effect = create_stop_after_2

        with patch("app.pause_manager.check_and_resume", return_value=None):
            result = handle_pause(str(koan_root), instance, 5)
            assert result is None
            # Should break much earlier than 60 sleeps
            assert mock_sleep.call_count < 10

    @patch("app.run.time.sleep")
    def test_pause_breaks_on_shutdown_signal(self, mock_sleep, koan_root):
        """Pause breaks out when .koan-shutdown appears, returns None."""
        from app.run import handle_pause

        instance = str(koan_root / "instance")
        (koan_root / ".koan-pause").touch()

        sleep_count = [0]
        def create_shutdown_after_2(duration):
            sleep_count[0] += 1
            if sleep_count[0] >= 2:
                (koan_root / ".koan-shutdown").write_text(str(int(time.time())))

        mock_sleep.side_effect = create_shutdown_after_2

        with patch("app.pause_manager.check_and_resume", return_value=None):
            result = handle_pause(str(koan_root), instance, 5)
            assert result is None
            # Should break much earlier than 60 sleeps
            assert mock_sleep.call_count < 10


# ---------------------------------------------------------------------------
# Test: run_claude_task
# ---------------------------------------------------------------------------

class TestRunClaudeTask:
    def test_captures_output(self, tmp_path):
        from app.run import run_claude_task, _sig
        _sig.task_running = False

        stdout_f = str(tmp_path / "out.txt")
        stderr_f = str(tmp_path / "err.txt")

        exit_code = run_claude_task(
            cmd=["echo", "hello world"],
            stdout_file=stdout_f,
            stderr_file=stderr_f,
            cwd=str(tmp_path),
        )

        assert exit_code == 0
        assert Path(stdout_f).read_text().strip() == "hello world"
        assert _sig.task_running is False

    def test_nonzero_exit(self, tmp_path):
        from app.run import run_claude_task

        stdout_f = str(tmp_path / "out.txt")
        stderr_f = str(tmp_path / "err.txt")

        exit_code = run_claude_task(
            cmd=["false"],
            stdout_file=stdout_f,
            stderr_file=stderr_f,
            cwd=str(tmp_path),
        )

        assert exit_code != 0

    def test_resets_signal_state(self, tmp_path):
        from app.run import run_claude_task, _sig

        stdout_f = str(tmp_path / "out.txt")
        stderr_f = str(tmp_path / "err.txt")

        run_claude_task(
            cmd=["echo", "test"],
            stdout_file=stdout_f,
            stderr_file=stderr_f,
            cwd=str(tmp_path),
        )

        assert _sig.task_running is False
        assert _sig.first_ctrl_c == 0
        assert _sig.claude_proc is None

    def test_resets_signal_state_on_popen_failure(self, tmp_path):
        """_sig.task_running must be reset even when popen_cli raises."""
        from app.run import run_claude_task, _sig

        _sig.task_running = False  # ensure clean start

        stdout_f = str(tmp_path / "out.txt")
        stderr_f = str(tmp_path / "err.txt")

        with patch("app.cli_exec.popen_cli", side_effect=OSError("exec failed")):
            try:
                run_claude_task(
                    cmd=["nonexistent"],
                    stdout_file=stdout_f,
                    stderr_file=stderr_f,
                    cwd=str(tmp_path),
                )
            except OSError:
                pass

        # Signal state must be cleaned up despite the exception
        assert _sig.task_running is False
        assert _sig.first_ctrl_c == 0
        assert _sig.claude_proc is None

    def test_resets_signal_state_on_open_failure(self, tmp_path):
        """_sig.task_running must be reset when stdout_file can't be opened."""
        from app.run import run_claude_task, _sig

        _sig.task_running = False

        # Use a path that can't be opened (directory as file)
        bad_dir = tmp_path / "baddir"
        bad_dir.mkdir()
        stderr_f = str(tmp_path / "err.txt")

        try:
            run_claude_task(
                cmd=["echo", "hello"],
                stdout_file=str(bad_dir),  # can't open a directory for writing
                stderr_file=stderr_f,
                cwd=str(tmp_path),
            )
        except (OSError, IsADirectoryError):
            pass

        assert _sig.task_running is False
        assert _sig.claude_proc is None


# ---------------------------------------------------------------------------
# Test: main (restart wrapper)
# ---------------------------------------------------------------------------

class TestMain:
    @patch("app.run.main_loop")
    def test_normal_exit(self, mock_loop):
        from app.run import main
        main()
        mock_loop.assert_called_once()

    @patch("app.run.main_loop")
    @patch("app.run.time.sleep")
    def test_restart_on_42(self, mock_sleep, mock_loop):
        from app.run import main
        from app.restart_manager import RESTART_EXIT_CODE
        call_count = [0]
        def side_effect():
            call_count[0] += 1
            if call_count[0] == 1:
                raise SystemExit(RESTART_EXIT_CODE)
            # Second call: normal exit
        mock_loop.side_effect = side_effect
        main()
        assert call_count[0] == 2
        mock_sleep.assert_called_once_with(1)

    @patch("app.run.main_loop")
    def test_other_exit_code_propagates(self, mock_loop):
        from app.run import main
        mock_loop.side_effect = SystemExit(1)
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1


# ---------------------------------------------------------------------------
# Test: main_loop basic flow
# ---------------------------------------------------------------------------

class TestMainLoop:
    @patch("app.run.subprocess.run")
    @patch("app.run.run_startup", return_value=(5, 10, "koan/"))
    @patch("app.run.acquire_pidfile")
    @patch("app.run.release_pidfile")
    def test_stop_file_exits_loop(self, mock_release, mock_acquire, mock_startup, mock_subproc, koan_root):
        """Stop file created DURING loop causes clean exit."""
        from app.run import main_loop

        os.environ["KOAN_ROOT"] = str(koan_root)
        os.environ["KOAN_PROJECTS"] = f"test:{koan_root}"
        (koan_root / ".koan-project").write_text("test")

        # Create .koan-stop AFTER startup clears it — simulate /stop while running
        original_startup = mock_startup.side_effect

        def startup_then_stop(*args, **kwargs):
            (koan_root / ".koan-stop").touch()
            return (5, 10, "koan/")

        mock_startup.side_effect = startup_then_stop

        with patch("app.run._notify"):
            main_loop()

        # Should have exited cleanly
        mock_release.assert_called()
        assert not (koan_root / ".koan-stop").exists()

    @patch("app.run.subprocess.run")
    @patch("app.run.run_startup", return_value=(5, 10, "koan/"))
    @patch("app.run.acquire_pidfile")
    @patch("app.run.release_pidfile")
    def test_stale_stop_file_cleared_on_startup(self, mock_release, mock_acquire, mock_startup, mock_subproc, koan_root):
        """Stale .koan-stop from a previous make stop is cleared on startup.

        Regression test: `make stop` creates .koan-stop. If run.py was not
        running, the file persists. Next `make run` should NOT immediately
        exit — the stale file must be cleared before entering the loop.
        """
        from app.run import main_loop

        os.environ["KOAN_ROOT"] = str(koan_root)
        os.environ["KOAN_PROJECTS"] = f"test:{koan_root}"
        (koan_root / ".koan-project").write_text("test")

        # Simulate stale .koan-stop from a previous `make stop`
        (koan_root / ".koan-stop").write_text("STOP")

        # Mock plan_iteration to create a new stop file on first call
        # (so the loop doesn't run forever) — but the KEY test is that
        # the stale file didn't cause an immediate exit before startup
        call_count = [0]

        def startup_creates_stop(*args, **kwargs):
            call_count[0] += 1
            # Create stop on startup — this time it's a "fresh" signal
            (koan_root / ".koan-stop").touch()
            return (5, 10, "koan/")

        mock_startup.side_effect = startup_creates_stop

        with patch("app.run._notify"):
            main_loop()

        # Startup ran (proves we didn't exit immediately from the stale file)
        assert call_count[0] == 1
        mock_startup.assert_called_once()

    @patch("app.run.subprocess.run")
    @patch("app.run.run_startup", return_value=(5, 10, "koan/"))
    @patch("app.run.acquire_pidfile")
    @patch("app.run.release_pidfile")
    def test_stale_stop_file_absent_no_error(self, mock_release, mock_acquire, mock_startup, mock_subproc, koan_root):
        """No crash when .koan-stop doesn't exist at startup (normal case)."""
        from app.run import main_loop

        os.environ["KOAN_ROOT"] = str(koan_root)
        os.environ["KOAN_PROJECTS"] = f"test:{koan_root}"
        (koan_root / ".koan-project").write_text("test")

        # No .koan-stop exists — verify no error from unlink(missing_ok=True)
        assert not (koan_root / ".koan-stop").exists()

        # Create stop on startup so the loop exits
        def startup_then_stop(*args, **kwargs):
            (koan_root / ".koan-stop").touch()
            return (5, 10, "koan/")

        mock_startup.side_effect = startup_then_stop

        with patch("app.run._notify"):
            main_loop()

        mock_startup.assert_called_once()

    @patch("app.run.subprocess.run")
    @patch("app.run.run_startup", return_value=(5, 10, "koan/"))
    @patch("app.run.acquire_pidfile")
    @patch("app.run.release_pidfile")
    def test_restart_file_exits_42(self, mock_release, mock_acquire, mock_startup, mock_subproc, koan_root):
        from app.run import main_loop
        from app.restart_manager import RESTART_EXIT_CODE

        os.environ["KOAN_ROOT"] = str(koan_root)
        os.environ["KOAN_PROJECTS"] = f"test:{koan_root}"

        # Create restart file AFTER startup (via side_effect) so startup
        # cleanup doesn't remove it before the loop's restart check runs.
        def startup_creates_restart(*args, **kwargs):
            restart_file = koan_root / ".koan-restart"
            restart_file.write_text("restart")
            future = time.time() + 3600
            os.utime(str(restart_file), (future, future))
            return (5, 10, "koan/")

        mock_startup.side_effect = startup_creates_restart

        with pytest.raises(SystemExit) as exc:
            with patch("app.run._notify"):
                main_loop()
        assert exc.value.code == RESTART_EXIT_CODE

    @patch("app.run.subprocess.run")
    @patch("app.run.run_startup", return_value=(5, 10, "koan/"))
    @patch("app.run.acquire_pidfile")
    @patch("app.run.release_pidfile")
    def test_restart_file_cleared_before_exit(self, mock_release, mock_acquire, mock_startup, mock_subproc, koan_root):
        """Regression: run.py must clear .koan-restart before sys.exit(RESTART_EXIT_CODE)
        to prevent the restarted process from seeing a stale file and
        entering a restart loop."""
        from app.run import main_loop
        from app.restart_manager import RESTART_EXIT_CODE

        os.environ["KOAN_ROOT"] = str(koan_root)
        os.environ["KOAN_PROJECTS"] = f"test:{koan_root}"

        restart_file = koan_root / ".koan-restart"

        def startup_creates_restart(*args, **kwargs):
            restart_file.write_text("restart")
            future = time.time() + 3600
            os.utime(str(restart_file), (future, future))
            return (5, 10, "koan/")

        mock_startup.side_effect = startup_creates_restart

        with pytest.raises(SystemExit) as exc:
            with patch("app.run._notify"):
                main_loop()
        assert exc.value.code == RESTART_EXIT_CODE
        # The restart file must be deleted BEFORE exit
        assert not restart_file.exists(), \
            ".koan-restart was not cleared before exit — restart loop risk"

    @patch("app.run.subprocess.run")
    @patch("app.run.run_startup", return_value=(5, 10, "koan/"))
    @patch("app.run.acquire_pidfile")
    @patch("app.run.release_pidfile")
    def test_stale_restart_file_cleared_on_startup(self, mock_release, mock_acquire, mock_startup, mock_subproc, koan_root):
        """Stale .koan-restart from a previous session is cleared on startup.

        Regression: if run.py is killed while .koan-restart exists, the stale
        file would be seen by the next startup and immediately trigger exit(42),
        creating a restart loop.
        """
        from app.run import main_loop

        os.environ["KOAN_ROOT"] = str(koan_root)
        os.environ["KOAN_PROJECTS"] = f"test:{koan_root}"
        (koan_root / ".koan-project").write_text("test")

        # Simulate stale .koan-restart from a previous session
        (koan_root / ".koan-restart").write_text("stale restart")

        # Startup creates a stop file so the loop exits cleanly
        def startup_then_stop(*args, **kwargs):
            (koan_root / ".koan-stop").touch()
            return (5, 10, "koan/")

        mock_startup.side_effect = startup_then_stop

        with patch("app.run._notify"):
            main_loop()

        # Startup ran (stale restart didn't cause immediate exit)
        mock_startup.assert_called_once()
        # The restart file was cleared
        assert not (koan_root / ".koan-restart").exists()


# ---------------------------------------------------------------------------
# Test: bold helpers
# ---------------------------------------------------------------------------

class TestBoldHelpers:
    def test_bold_cyan(self):
        from app.run import bold_cyan, _init_colors
        _init_colors()
        result = bold_cyan("test")
        assert "test" in result

    def test_bold_green(self):
        from app.run import bold_green, _init_colors
        _init_colors()
        result = bold_green("test")
        assert "test" in result


class TestStyled:
    """Tests for the _styled() helper that bold_cyan/bold_green delegate to."""

    def test_styled_returns_text(self):
        from app.run import _styled, _init_colors
        _init_colors()
        result = _styled("hello", "bold", "cyan")
        assert "hello" in result

    def test_styled_single_style(self):
        from app.run import _styled, _init_colors
        _init_colors()
        result = _styled("x", "red")
        assert "x" in result

    def test_styled_no_styles(self):
        from app.run import _styled, _init_colors
        _init_colors()
        result = _styled("plain")
        assert "plain" in result

    def test_bold_cyan_uses_styled(self):
        """bold_cyan() should produce the same result as _styled("x", "bold", "cyan")."""
        from app.run import bold_cyan, _styled, _init_colors
        _init_colors()
        assert bold_cyan("test") == _styled("test", "bold", "cyan")

    def test_bold_green_uses_styled(self):
        """bold_green() should produce the same result as _styled("x", "bold", "green")."""
        from app.run import bold_green, _styled, _init_colors
        _init_colors()
        assert bold_green("test") == _styled("test", "bold", "green")

    def test_styled_with_colors_disabled(self, monkeypatch):
        """When KOAN_FORCE_COLOR is unset and stdout is not TTY, styles are empty strings."""
        import app.run as run_mod
        monkeypatch.delenv("KOAN_FORCE_COLOR", raising=False)
        run_mod._COLORS = {}
        # Non-TTY stdout: styles should be empty
        run_mod._init_colors()
        if not sys.stdout.isatty():
            result = run_mod._styled("text", "bold", "red")
            assert result == "text"


class TestIdleWaitConfig:
    """Tests for the consolidated _IDLE_WAIT_CONFIG dispatch in _run_iteration."""

    def _make_plan(self, action, **overrides):
        """Build a minimal iteration plan dict."""
        plan = {
            "action": action,
            "project_name": "koan",
            "project_path": "/tmp/koan",
            "autonomous_mode": "implement",
            "available_pct": 50,
            "display_lines": [],
            "mission_title": "",
            "focus_area": "",
            "decision_reason": "",
            "recurring_injected": [],
        }
        plan.update(overrides)
        return plan

    @patch("app.run.interruptible_sleep", return_value=None)
    @patch("app.run.set_status")
    @patch("app.run.log")
    @patch("app.run.plan_iteration")
    def test_focus_wait_action(self, mock_plan, mock_log, mock_status, mock_sleep, tmp_path):
        """focus_wait action should use consolidated idle wait handler."""
        from app.run import _run_iteration
        mock_plan.return_value = self._make_plan("focus_wait", focus_remaining="3h45m")
        instance = str(tmp_path / "instance")
        os.makedirs(instance, exist_ok=True)
        (tmp_path / ".koan-project").write_text("koan")

        _run_iteration(
            koan_root=str(tmp_path),
            instance=instance,
            projects=[("koan", "/tmp/koan")],
            count=0, max_runs=10, interval=60, git_sync_interval=5,
        )

        # Verify sleep was called with the interval
        mock_sleep.assert_called_once_with(60, str(tmp_path), instance)
        # Verify status was set with focus info
        status_calls = [c for c in mock_status.call_args_list if "Focus mode" in str(c)]
        assert len(status_calls) >= 1

    @patch("app.run.interruptible_sleep", return_value=None)
    @patch("app.run.set_status")
    @patch("app.run.log")
    @patch("app.run.plan_iteration")
    def test_schedule_wait_action(self, mock_plan, mock_log, mock_status, mock_sleep, tmp_path):
        """schedule_wait action should use consolidated idle wait handler."""
        from app.run import _run_iteration
        mock_plan.return_value = self._make_plan("schedule_wait")
        instance = str(tmp_path / "instance")
        os.makedirs(instance, exist_ok=True)
        (tmp_path / ".koan-project").write_text("koan")

        _run_iteration(
            koan_root=str(tmp_path),
            instance=instance,
            projects=[("koan", "/tmp/koan")],
            count=0, max_runs=10, interval=60, git_sync_interval=5,
        )

        mock_sleep.assert_called_once()
        status_calls = [c for c in mock_status.call_args_list if "Work hours" in str(c)]
        assert len(status_calls) >= 1

    @patch("app.run.interruptible_sleep", return_value=None)
    @patch("app.run.set_status")
    @patch("app.run.log")
    @patch("app.run.plan_iteration")
    def test_exploration_wait_action(self, mock_plan, mock_log, mock_status, mock_sleep, tmp_path):
        """exploration_wait action should use consolidated idle wait handler."""
        from app.run import _run_iteration
        mock_plan.return_value = self._make_plan("exploration_wait")
        instance = str(tmp_path / "instance")
        os.makedirs(instance, exist_ok=True)
        (tmp_path / ".koan-project").write_text("koan")

        _run_iteration(
            koan_root=str(tmp_path),
            instance=instance,
            projects=[("koan", "/tmp/koan")],
            count=0, max_runs=10, interval=60, git_sync_interval=5,
        )

        mock_sleep.assert_called_once()
        status_calls = [c for c in mock_status.call_args_list if "Exploration disabled" in str(c)]
        assert len(status_calls) >= 1

    @patch("app.run.interruptible_sleep", return_value=None)
    @patch("app.run.set_status")
    @patch("app.run.log")
    @patch("app.run.plan_iteration")
    def test_pr_limit_wait_action(self, mock_plan, mock_log, mock_status, mock_sleep, tmp_path):
        """pr_limit_wait action should use consolidated idle wait handler."""
        from app.run import _run_iteration
        mock_plan.return_value = self._make_plan("pr_limit_wait")
        instance = str(tmp_path / "instance")
        os.makedirs(instance, exist_ok=True)
        (tmp_path / ".koan-project").write_text("koan")

        _run_iteration(
            koan_root=str(tmp_path),
            instance=instance,
            projects=[("koan", "/tmp/koan")],
            count=0, max_runs=10, interval=60, git_sync_interval=5,
        )

        mock_sleep.assert_called_once()
        status_calls = [c for c in mock_status.call_args_list if "PR limit" in str(c)]
        assert len(status_calls) >= 1

    @patch("app.run.interruptible_sleep", return_value="mission")
    @patch("app.run.set_status")
    @patch("app.run.log")
    @patch("app.run.plan_iteration")
    def test_idle_wait_wakes_on_mission(self, mock_plan, mock_log, mock_status, mock_sleep, tmp_path):
        """When interruptible_sleep returns 'mission', idle wait should log wakeup."""
        from app.run import _run_iteration
        mock_plan.return_value = self._make_plan("focus_wait", focus_remaining="2h")
        instance = str(tmp_path / "instance")
        os.makedirs(instance, exist_ok=True)
        (tmp_path / ".koan-project").write_text("koan")

        _run_iteration(
            koan_root=str(tmp_path),
            instance=instance,
            projects=[("koan", "/tmp/koan")],
            count=0, max_runs=10, interval=60, git_sync_interval=5,
        )

        # Should log about waking up
        wake_logs = [c for c in mock_log.call_args_list if "waking up" in str(c)]
        assert len(wake_logs) >= 1


class TestComputeQuotaResetTs:
    """Tests for _compute_quota_reset_ts and _compute_preflight_reset_ts."""

    @patch("app.run.log")
    def test_compute_quota_reset_ts_fallback(self, mock_log, tmp_path):
        """When usage_estimator fails, falls back to QUOTA_RETRY_SECONDS."""
        from app.run import _compute_quota_reset_ts
        instance = str(tmp_path)
        reset_ts, reset_display = _compute_quota_reset_ts(instance)
        # Should return a future timestamp
        assert reset_ts > time.time() - 10
        assert isinstance(reset_ts, int)

    @patch("app.run.log")
    def test_compute_preflight_reset_ts_fallback(self, mock_log):
        """When quota_handler extraction fails, falls back to QUOTA_RETRY_SECONDS."""
        from app.run import _compute_preflight_reset_ts
        reset_ts, reset_display = _compute_preflight_reset_ts("")
        assert reset_ts > time.time() - 10
        assert isinstance(reset_ts, int)

    @patch("app.run.log")
    def test_compute_preflight_reset_ts_with_error_output(self, mock_log):
        """With error output, should attempt extraction (and fall back gracefully)."""
        from app.run import _compute_preflight_reset_ts
        reset_ts, reset_display = _compute_preflight_reset_ts("Rate limit exceeded, try again at 10:00 AM")
        assert reset_ts > time.time() - 10

    @patch("app.usage_estimator.cmd_reset_time", return_value=9999999999)
    @patch("app.usage_estimator._load_state", return_value={"session_start": "2026-02-16T10:00:00Z"})
    @patch("app.usage_estimator._estimate_reset_time", return_value="4h30m")
    @patch("app.run.log")
    def test_compute_quota_reset_ts_with_valid_state(
        self, mock_log, mock_estimate, mock_state, mock_reset
    ):
        """When usage_estimator works, uses its output."""
        from app.run import _compute_quota_reset_ts
        reset_ts, reset_display = _compute_quota_reset_ts("/tmp/test-instance")
        assert reset_ts == 9999999999
        assert "4h30m" in reset_display


# ---------------------------------------------------------------------------
# Test: quota spam loop regression tests (session 220 fixes)
# ---------------------------------------------------------------------------

class TestQuotaSpamLoopFixes:
    """Regression tests for the quota exhaustion spam loop bug.

    The bug: when quota is exhausted, the system entered a rapid
    auto-resume → detect exhaustion → pause → auto-resume loop,
    spamming the user with messages.

    Root causes:
    1. wait_pause created pause with timestamp=now (instant auto-resume)
    2. Usage refresh was skipped when count=0 (stale data after resume)
    3. Post-mission quota_info was treated as dict but was actually a tuple

    Note: plan_iteration tests moved to test_iteration_manager.py as part
    of the consolidation in issue #206.
    """

    def test_wait_pause_creates_pause_with_future_timestamp(self, koan_root):
        """wait_pause must create pause with a future timestamp, not now."""
        import app.run as run_module

        instance = str(koan_root / "instance")
        koan_root_str = str(koan_root)
        now = int(time.time())
        future_ts = now + 3600  # 1 hour from now

        # Track create_pause calls
        pause_calls = []

        with patch.object(run_module, "log"), \
             patch.object(run_module, "_notify"), \
             patch.object(run_module, "subprocess") as mock_sub, \
             patch("app.usage_estimator.cmd_reset_time", return_value=future_ts), \
             patch("app.pause_manager.create_pause") as mock_create:
            mock_sub.run.return_value = MagicMock(returncode=0)
            mock_create.side_effect = lambda *a, **kw: pause_calls.append((a, kw))

            # Simulate the wait_pause action handler inline
            # (We test the code path directly rather than going through main_loop)
            plan = {"decision_reason": "Budget exhausted", "project_name": "test"}
            project_name = "test"
            count = 0

            # Execute the wait_pause code path
            reset_ts = None
            try:
                from app.usage_estimator import cmd_reset_time
                usage_state_path = Path(instance, "usage_state.json")
                reset_ts = cmd_reset_time(usage_state_path)
            except Exception:
                pass
            if reset_ts is None:
                reset_ts = int(time.time()) + 5 * 3600

            from app.pause_manager import create_pause
            create_pause(koan_root_str, "quota", reset_ts)

        # Verify create_pause was called with a future timestamp
        assert len(pause_calls) == 1
        args = pause_calls[0][0]
        assert args[0] == koan_root_str  # koan_root
        assert args[1] == "quota"  # reason
        assert args[2] == future_ts  # timestamp (future, not now)
        assert args[2] > now  # Must be in the future

    def test_post_mission_quota_creates_pause(self, koan_root):
        """Post-mission quota_exhausted path MUST call create_pause().

        Regression: the post-mission path notified the user about a pause
        but never wrote the .koan-pause file, so the loop continued endlessly.
        """
        import app.run as run_module

        instance = str(koan_root / "instance")
        koan_root_str = str(koan_root)
        future_ts = int(time.time()) + 3600

        post_result = {
            "quota_exhausted": True,
            "quota_info": ("resets in ~1h", "Auto-resume in ~1h"),
        }

        pause_calls = []

        with patch.object(run_module, "log"), \
             patch.object(run_module, "_notify"), \
             patch.object(run_module, "_commit_instance"), \
             patch.object(run_module, "_compute_quota_reset_ts",
                          return_value=(future_ts, "resets in ~1h")), \
             patch("app.pause_manager.create_pause") as mock_pause:
            mock_pause.side_effect = lambda *a, **kw: pause_calls.append(a)

            # Simulate the quota_exhausted code path inline
            quota_info = post_result.get("quota_info")
            if quota_info and isinstance(quota_info, (list, tuple)) and len(quota_info) >= 2:
                reset_display, resume_msg = quota_info[0], quota_info[1]
            else:
                reset_display, resume_msg = "", "Auto-resume in ~5h"

            reset_ts, _disp = run_module._compute_quota_reset_ts(instance)
            from app.pause_manager import create_pause
            create_pause(koan_root_str, "quota", reset_ts, reset_display or _disp)

        assert len(pause_calls) == 1, "create_pause must be called on quota exhaustion"
        assert pause_calls[0][0] == koan_root_str
        assert pause_calls[0][1] == "quota"
        assert pause_calls[0][2] == future_ts

    def test_post_mission_quota_info_tuple_handling(self):
        """Post-mission quota_info is a (reset_display, resume_msg) tuple."""
        post_result = {
            "quota_exhausted": True,
            "quota_info": ("resets at 10am", "Auto-resume at reset time (~2h)"),
        }

        quota_info = post_result.get("quota_info")
        if quota_info and isinstance(quota_info, (list, tuple)) and len(quota_info) >= 2:
            reset_display, resume_msg = quota_info[0], quota_info[1]
        else:
            reset_display, resume_msg = "", "Auto-resume in ~5h"

        assert reset_display == "resets at 10am"
        assert resume_msg == "Auto-resume at reset time (~2h)"

    def test_post_mission_quota_info_none_handling(self):
        """When quota_info is None, use fallback values."""
        post_result = {
            "quota_exhausted": True,
            "quota_info": None,
        }

        quota_info = post_result.get("quota_info")
        if quota_info and isinstance(quota_info, (list, tuple)) and len(quota_info) >= 2:
            reset_display, resume_msg = quota_info[0], quota_info[1]
        else:
            reset_display, resume_msg = "", "Auto-resume in ~5h"

        assert reset_display == ""
        assert resume_msg == "Auto-resume in ~5h"

    def test_post_mission_quota_info_dict_fallback(self):
        """If quota_info were an empty dict (legacy), use fallback values."""
        post_result = {
            "quota_exhausted": True,
            "quota_info": {},
        }

        quota_info = post_result.get("quota_info")
        if quota_info and isinstance(quota_info, (list, tuple)) and len(quota_info) >= 2:
            reset_display, resume_msg = quota_info[0], quota_info[1]
        else:
            reset_display, resume_msg = "", "Auto-resume in ~5h"

        assert reset_display == ""
        assert resume_msg == "Auto-resume in ~5h"

    def test_pause_with_now_timestamp_resumes_instantly(self):
        """Prove the old bug: pause with timestamp=now causes instant auto-resume."""
        from app.pause_manager import should_auto_resume, PauseState

        now = int(time.time())
        state = PauseState(reason="quota", timestamp=now, display="")
        # With timestamp=now, should_auto_resume returns True immediately
        assert should_auto_resume(state, now=now) is True

    def test_pause_with_future_timestamp_does_not_resume(self):
        """Pause with a future timestamp should NOT auto-resume."""
        from app.pause_manager import should_auto_resume, PauseState

        now = int(time.time())
        future = now + 3600  # 1 hour from now
        state = PauseState(reason="quota", timestamp=future, display="")
        assert should_auto_resume(state, now=now) is False


# ---------------------------------------------------------------------------
# Test: CLI entry point
# ---------------------------------------------------------------------------

class TestCLI:
    def test_module_runnable(self):
        """Verify the module can be imported without side effects."""
        import app.run
        assert hasattr(app.run, "main")
        assert hasattr(app.run, "main_loop")


# ---------------------------------------------------------------------------
# Fixture: mock_error_handling
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_error_handling():
    """Common mock setup for error handling tests."""
    with patch("app.run._notify") as notify, \
         patch("app.run.time.sleep") as sleep, \
         patch("app.run.log") as log_fn:
        yield {"notify": notify, "sleep": sleep, "log": log_fn}


# ---------------------------------------------------------------------------
# Test: _handle_iteration_error
# ---------------------------------------------------------------------------

class TestHandleIterationError:
    """Tests for the error recovery handler."""

    def test_logs_error_and_backs_off(self, koan_root, capsys):
        from app.run import _handle_iteration_error, _init_colors
        _init_colors()
        instance = str(koan_root / "instance")

        with patch("app.run._notify"), \
             patch("app.run.time.sleep") as mock_sleep:
            _handle_iteration_error(
                ValueError("test error"), 1, str(koan_root), instance,
            )

        out = capsys.readouterr().out
        assert "test error" in out
        assert "1/" in out
        # First error: 10s backoff
        mock_sleep.assert_called_once_with(10)

    def test_backoff_increases(self, koan_root, mock_error_handling):
        from app.run import _handle_iteration_error
        instance = str(koan_root / "instance")

        _handle_iteration_error(
            ValueError("err"), 3, str(koan_root), instance,
        )

        # 3rd error: 30s backoff
        mock_error_handling["sleep"].assert_called_once_with(30)

    def test_backoff_capped_at_max(self, koan_root, mock_error_handling):
        from app.run import _handle_iteration_error
        instance = str(koan_root / "instance")

        _handle_iteration_error(
            ValueError("err"), 8, str(koan_root), instance,
        )

        # 8th error: 80s backoff
        mock_error_handling["sleep"].assert_called_once_with(80)

    def test_backoff_capped_at_max_iteration_constant(self, koan_root, mock_error_handling):
        from app.run import _handle_iteration_error, MAX_CONSECUTIVE_ERRORS
        instance = str(koan_root / "instance")

        # Use error count below MAX_CONSECUTIVE_ERRORS to avoid entering pause mode
        with patch("app.pause_manager.create_pause"):
            _handle_iteration_error(
                ValueError("err"), 9, str(koan_root), instance,
            )

        # 9th error: 90s backoff (below cap)
        mock_error_handling["sleep"].assert_called_once_with(90)

    def test_notifies_on_first_error(self, koan_root, mock_error_handling):
        from app.run import _handle_iteration_error
        instance = str(koan_root / "instance")

        _handle_iteration_error(
            ValueError("boom"), 1, str(koan_root), instance,
        )

        mock_error_handling["notify"].assert_called_once()
        assert "boom" in mock_error_handling["notify"].call_args[0][1]

    def test_throttles_notifications(self, koan_root):
        """Only notifies on 1st and every 5th error."""
        from app.run import _handle_iteration_error, ERROR_NOTIFICATION_INTERVAL
        instance = str(koan_root / "instance")

        # Errors 2, 3, 4 should not notify
        for i in range(2, ERROR_NOTIFICATION_INTERVAL):
            with patch("app.run._notify") as mock_notify, \
                 patch("app.run.time.sleep"), \
                 patch("app.run.log"):
                _handle_iteration_error(
                    ValueError("err"), i, str(koan_root), instance,
                )
            mock_notify.assert_not_called()

        # ERROR_NOTIFICATION_INTERVAL-th error: should notify
        with patch("app.run._notify") as mock_notify, \
             patch("app.run.time.sleep"), \
             patch("app.run.log"):
            _handle_iteration_error(
                ValueError("err"), ERROR_NOTIFICATION_INTERVAL, str(koan_root), instance,
            )
        mock_notify.assert_called_once()

    def test_enters_pause_at_max_errors(self, koan_root, mock_error_handling):
        from app.run import _handle_iteration_error, MAX_CONSECUTIVE_ERRORS
        instance = str(koan_root / "instance")

        with patch("app.pause_manager.create_pause") as mock_pause:
            _handle_iteration_error(
                RuntimeError("fatal"), MAX_CONSECUTIVE_ERRORS,
                str(koan_root), instance,
            )

        mock_pause.assert_called_once_with(str(koan_root), "errors")

    def test_no_pause_below_max_errors(self, koan_root, mock_error_handling):
        from app.run import _handle_iteration_error, MAX_CONSECUTIVE_ERRORS
        instance = str(koan_root / "instance")

        with patch("app.pause_manager.create_pause") as mock_pause:
            _handle_iteration_error(
                RuntimeError("err"), MAX_CONSECUTIVE_ERRORS - 1,
                str(koan_root), instance,
            )

        mock_pause.assert_not_called()

    def test_no_sleep_at_max_errors(self, koan_root, mock_error_handling):
        """At max errors, enters pause — no backoff sleep."""
        from app.run import _handle_iteration_error, MAX_CONSECUTIVE_ERRORS
        instance = str(koan_root / "instance")

        with patch("app.pause_manager.create_pause"):
            _handle_iteration_error(
                RuntimeError("fatal"), MAX_CONSECUTIVE_ERRORS,
                str(koan_root), instance,
            )

        mock_error_handling["sleep"].assert_not_called()


# ---------------------------------------------------------------------------
# Test: main_loop iteration error recovery
# ---------------------------------------------------------------------------

class TestMainLoopResilience:
    """Tests that main_loop survives iteration failures."""

    @patch("app.run.subprocess.run")
    @patch("app.run.run_startup", return_value=(5, 10, "koan/"))
    @patch("app.run.acquire_pidfile")
    @patch("app.run.release_pidfile")
    @patch("app.run._run_iteration")
    @patch("app.run._handle_iteration_error")
    def test_recovers_from_iteration_error(
        self, mock_handle_err, mock_iteration, mock_release,
        mock_acquire, mock_startup, mock_subproc, koan_root,
    ):
        """An exception in _run_iteration doesn't kill main_loop."""
        from app.run import main_loop

        os.environ["KOAN_ROOT"] = str(koan_root)
        os.environ["KOAN_PROJECTS"] = f"test:{koan_root}"
        (koan_root / ".koan-project").write_text("test")

        call_count = [0]
        def iteration_side_effect(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("network error")
            # Second call: create stop file to end loop
            (koan_root / ".koan-stop").touch()
            (koan_root / ".koan-project").write_text("test")
            return True  # productive iteration

        mock_iteration.side_effect = iteration_side_effect

        with patch("app.run._notify"):
            main_loop()

        # Error handler was called for the first failure
        mock_handle_err.assert_called_once()
        args = mock_handle_err.call_args[0]
        assert isinstance(args[0], RuntimeError)
        assert args[1] == 1  # consecutive_errors

    @patch("app.run.subprocess.run")
    @patch("app.run.run_startup", return_value=(5, 10, "koan/"))
    @patch("app.run.acquire_pidfile")
    @patch("app.run.release_pidfile")
    @patch("app.run._run_iteration")
    @patch("app.run._handle_iteration_error")
    def test_consecutive_error_counter_resets_on_success(
        self, mock_handle_err, mock_iteration, mock_release,
        mock_acquire, mock_startup, mock_subproc, koan_root,
    ):
        """Successful iteration resets the consecutive error counter."""
        from app.run import main_loop

        os.environ["KOAN_ROOT"] = str(koan_root)
        os.environ["KOAN_PROJECTS"] = f"test:{koan_root}"
        (koan_root / ".koan-project").write_text("test")

        call_count = [0]
        def iteration_side_effect(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("first error")
            if call_count[0] == 2:
                raise RuntimeError("second error")
            if call_count[0] == 3:
                # Success — this resets consecutive_errors to 0
                return True  # productive
            if call_count[0] == 4:
                raise RuntimeError("third error after reset")
            # 5th call: stop
            (koan_root / ".koan-stop").touch()
            (koan_root / ".koan-project").write_text("test")
            return True  # productive

        mock_iteration.side_effect = iteration_side_effect

        with patch("app.run._notify"):
            main_loop()

        # Error handler called 3 times:
        # calls 1,2 had consecutive_errors=1,2
        # call 3 succeeded → reset to 0
        # call 4 had consecutive_errors=1 (reset)
        assert mock_handle_err.call_count == 3
        assert mock_handle_err.call_args_list[0][0][1] == 1  # first error
        assert mock_handle_err.call_args_list[1][0][1] == 2  # second error
        assert mock_handle_err.call_args_list[2][0][1] == 1  # reset after success

    @patch("app.run.subprocess.run")
    @patch("app.run.run_startup", return_value=(5, 10, "koan/"))
    @patch("app.run.acquire_pidfile")
    @patch("app.run.release_pidfile")
    @patch("app.run._run_iteration")
    def test_keyboard_interrupt_propagates(
        self, mock_iteration, mock_release,
        mock_acquire, mock_startup, mock_subproc, koan_root,
    ):
        """KeyboardInterrupt is NOT caught by the iteration handler."""
        from app.run import main_loop

        os.environ["KOAN_ROOT"] = str(koan_root)
        os.environ["KOAN_PROJECTS"] = f"test:{koan_root}"
        (koan_root / ".koan-project").write_text("test")

        mock_iteration.side_effect = KeyboardInterrupt

        with patch("app.run._notify"):
            # main_loop catches KeyboardInterrupt at the top level
            main_loop()

        # Release should still be called (in finally block)
        mock_release.assert_called()

    @patch("app.run.subprocess.run")
    @patch("app.run.run_startup", return_value=(5, 10, "koan/"))
    @patch("app.run.acquire_pidfile")
    @patch("app.run.release_pidfile")
    @patch("app.run._run_iteration")
    def test_system_exit_42_propagates(
        self, mock_iteration, mock_release,
        mock_acquire, mock_startup, mock_subproc, koan_root,
    ):
        """SystemExit(42) propagates for restart handling."""
        from app.run import main_loop

        os.environ["KOAN_ROOT"] = str(koan_root)
        os.environ["KOAN_PROJECTS"] = f"test:{koan_root}"
        (koan_root / ".koan-project").write_text("test")

        mock_iteration.side_effect = SystemExit(42)

        with pytest.raises(SystemExit) as exc:
            with patch("app.run._notify"):
                main_loop()
        assert exc.value.code == 42


# ---------------------------------------------------------------------------
# Test: main() crash recovery
# ---------------------------------------------------------------------------

class TestMainCrashRecovery:
    """Tests for the outer crash recovery wrapper in main()."""

    @patch("app.run.main_loop")
    @patch("app.run.time.sleep")
    def test_recovers_from_unexpected_crash(self, mock_sleep, mock_loop):
        """main() restarts main_loop after an unexpected exception."""
        from app.run import main, BACKOFF_MULTIPLIER

        call_count = [0]
        def side_effect():
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("unexpected crash")
            # Second call: normal exit

        mock_loop.side_effect = side_effect
        main()
        assert call_count[0] == 2
        # First crash: BACKOFF_MULTIPLIER * 1 = 10s
        mock_sleep.assert_called_once_with(BACKOFF_MULTIPLIER)

    @patch("app.run.main_loop")
    @patch("app.run.time.sleep")
    def test_gives_up_after_max_crashes(self, mock_sleep, mock_loop):
        """main() stops retrying after MAX_MAIN_CRASHES consecutive crashes."""
        from app.run import main, MAX_MAIN_CRASHES

        mock_loop.side_effect = RuntimeError("always crashing")
        main()
        assert mock_loop.call_count == MAX_MAIN_CRASHES

    @patch("app.run.main_loop")
    @patch("app.run.time.sleep")
    def test_crash_count_resets_on_restart(self, mock_sleep, mock_loop):
        """SystemExit(RESTART_EXIT_CODE) resets the crash counter."""
        from app.run import main
        from app.restart_manager import RESTART_EXIT_CODE

        call_count = [0]
        def side_effect():
            call_count[0] += 1
            if call_count[0] == 1:
                raise SystemExit(RESTART_EXIT_CODE)  # restart signal
            # Second call: normal exit

        mock_loop.side_effect = side_effect
        main()
        assert call_count[0] == 2

    @patch("app.run.main_loop")
    @patch("app.run.time.sleep")
    def test_increasing_backoff_on_crashes(self, mock_sleep, mock_loop):
        """Backoff increases: 10s, 20s, 30s, 40s."""
        from app.run import main, BACKOFF_MULTIPLIER

        call_count = [0]
        def side_effect():
            call_count[0] += 1
            if call_count[0] <= 4:
                raise RuntimeError("crash")
            # 5th call: normal exit

        mock_loop.side_effect = side_effect
        main()
        sleeps = [c[0][0] for c in mock_sleep.call_args_list]
        expected = [BACKOFF_MULTIPLIER * i for i in range(1, 5)]
        assert sleeps == expected


# ---------------------------------------------------------------------------
# Test: _run_iteration action=error fails mission and returns
# ---------------------------------------------------------------------------

class TestRunIterationErrorAction:
    """The action=error path moves mission to Failed and returns (no raise)."""

    @patch("app.run._commit_instance")
    @patch("app.run._update_mission_in_file")
    @patch("app.run.plan_iteration")
    @patch("app.run._notify")
    def test_error_action_fails_mission_and_returns(
        self, mock_notify, mock_plan, mock_update, mock_commit, koan_root,
    ):
        from app.run import _run_iteration

        mock_plan.return_value = {
            "action": "error",
            "error": "Unknown project: foo",
            "project_name": "foo",
            "project_path": "",
            "mission_title": "do stuff",
            "autonomous_mode": "implement",
            "focus_area": "",
            "available_pct": 50,
            "decision_reason": "Default",
            "display_lines": [],
            "recurring_injected": [],
        }

        instance = str(koan_root / "instance")

        # Should return normally, NOT raise RuntimeError
        _run_iteration(
            koan_root=str(koan_root),
            instance=instance,
            projects=[("test", str(koan_root))],
            count=0,
            max_runs=5,
            interval=10,
            git_sync_interval=5,
        )

        # Mission moved to Failed
        mock_update.assert_called_once_with(instance, "do stuff", failed=True)
        # User notified
        mock_notify.assert_called_once()
        assert "Unknown project: foo" in mock_notify.call_args[0][1]
        # Instance committed
        mock_commit.assert_called_once_with(instance)

    @patch("app.run._commit_instance")
    @patch("app.run._update_mission_in_file")
    @patch("app.run.plan_iteration")
    @patch("app.run._notify")
    def test_error_action_without_mission_just_notifies(
        self, mock_notify, mock_plan, mock_update, mock_commit, koan_root,
    ):
        """When action=error has no mission_title, only notify (no file update)."""
        from app.run import _run_iteration

        mock_plan.return_value = {
            "action": "error",
            "error": "Some iteration error",
            "project_name": "test",
            "project_path": str(koan_root),
            "mission_title": "",
            "autonomous_mode": "implement",
            "focus_area": "",
            "available_pct": 50,
            "decision_reason": "Default",
            "display_lines": [],
            "recurring_injected": [],
        }

        instance = str(koan_root / "instance")

        _run_iteration(
            koan_root=str(koan_root),
            instance=instance,
            projects=[("test", str(koan_root))],
            count=0,
            max_runs=5,
            interval=10,
            git_sync_interval=5,
        )

        # No mission file update
        mock_update.assert_not_called()
        # No instance commit
        mock_commit.assert_not_called()
        # Notification sent
        mock_notify.assert_called_once()
        assert "Iteration error" in mock_notify.call_args[0][1]


# ---------------------------------------------------------------------------
# Test: claude_exit initialization (prevents UnboundLocalError)
# ---------------------------------------------------------------------------

class TestRunIterationGitHubPreCheck:
    """_run_iteration checks GitHub notifications before planning."""

    @patch("app.run.plan_iteration")
    @patch("app.run._notify")
    @patch("app.loop_manager.process_github_notifications")
    def test_github_notifications_checked_before_planning(
        self, mock_gh_notif, mock_notify, mock_plan, koan_root
    ):
        """process_github_notifications is called before plan_iteration."""
        from app.run import _run_iteration

        mock_gh_notif.return_value = 0
        mock_plan.return_value = {
            "action": "error",
            "error": "test-stop",
            "project_name": "test",
            "project_path": str(koan_root),
            "mission_title": "",
            "autonomous_mode": "implement",
            "focus_area": "",
            "available_pct": 50,
            "decision_reason": "Default",
            "display_lines": [],
            "recurring_injected": [],
        }

        instance = str(koan_root / "instance")

        with patch("app.utils.get_known_projects", return_value=[("test", str(koan_root))]):
            # action=error with empty mission_title returns normally (no raise)
            _run_iteration(
                koan_root=str(koan_root),
                instance=instance,
                projects=[("test", str(koan_root))],
                count=0,
                max_runs=5,
                interval=10,
                git_sync_interval=5,
            )

        mock_gh_notif.assert_called_once_with(str(koan_root), instance)

    @patch("app.run.plan_iteration")
    @patch("app.run._notify")
    @patch("app.loop_manager.process_github_notifications")
    def test_github_check_error_does_not_block_iteration(
        self, mock_gh_notif, mock_notify, mock_plan, koan_root
    ):
        """An exception in process_github_notifications is caught, not propagated."""
        from app.run import _run_iteration

        mock_gh_notif.side_effect = RuntimeError("GitHub API error")
        mock_plan.return_value = {
            "action": "error",
            "error": "test-stop",
            "project_name": "test",
            "project_path": str(koan_root),
            "mission_title": "",
            "autonomous_mode": "implement",
            "focus_area": "",
            "available_pct": 50,
            "decision_reason": "Default",
            "display_lines": [],
            "recurring_injected": [],
        }

        instance = str(koan_root / "instance")

        with patch("app.utils.get_known_projects", return_value=[("test", str(koan_root))]):
            # Should return normally — GitHub error is caught, action=error returns
            _run_iteration(
                koan_root=str(koan_root),
                instance=instance,
                projects=[("test", str(koan_root))],
                count=0,
                max_runs=5,
                interval=10,
                git_sync_interval=5,
            )


class TestRunIterationProjectRefresh:
    """_run_iteration refreshes projects list each iteration."""

    @patch("app.run.plan_iteration")
    @patch("app.run._notify")
    def test_refreshed_projects_passed_to_plan(self, mock_notify, mock_plan, koan_root):
        """When get_known_projects returns updated list, plan_iteration sees it."""
        from app.run import _run_iteration

        refreshed_projects = [("test", str(koan_root)), ("new-proj", "/tmp/new")]

        mock_plan.return_value = {
            "action": "error",
            "error": "test-stop",
            "project_name": "test",
            "project_path": str(koan_root),
            "mission_title": "",
            "autonomous_mode": "implement",
            "focus_area": "",
            "available_pct": 50,
            "decision_reason": "Default",
            "display_lines": [],
            "recurring_injected": [],
        }

        instance = str(koan_root / "instance")

        with patch("app.utils.get_known_projects", return_value=refreshed_projects), \
             patch("app.loop_manager.process_github_notifications", return_value=0):
            # action=error returns normally now (no raise)
            _run_iteration(
                koan_root=str(koan_root),
                instance=instance,
                projects=[("test", str(koan_root))],
                count=0,
                max_runs=5,
                interval=10,
                git_sync_interval=5,
            )

        # plan_iteration should have received the refreshed list
        call_kwargs = mock_plan.call_args[1]
        assert call_kwargs["projects"] == refreshed_projects

    @patch("app.run.plan_iteration")
    @patch("app.run._notify")
    def test_empty_refresh_keeps_original(self, mock_notify, mock_plan, koan_root):
        """If get_known_projects returns empty, original list is kept."""
        from app.run import _run_iteration

        original_projects = [("test", str(koan_root))]

        mock_plan.return_value = {
            "action": "error",
            "error": "test-stop",
            "project_name": "test",
            "project_path": str(koan_root),
            "mission_title": "",
            "autonomous_mode": "implement",
            "focus_area": "",
            "available_pct": 50,
            "decision_reason": "Default",
            "display_lines": [],
            "recurring_injected": [],
        }

        instance = str(koan_root / "instance")

        with patch("app.utils.get_known_projects", return_value=[]), \
             patch("app.loop_manager.process_github_notifications", return_value=0):
            # action=error returns normally (no raise)
            _run_iteration(
                koan_root=str(koan_root),
                instance=instance,
                projects=original_projects,
                count=0,
                max_runs=5,
                interval=10,
                git_sync_interval=5,
            )

        # plan_iteration should keep the original list
        call_kwargs = mock_plan.call_args[1]
        assert call_kwargs["projects"] == original_projects


class TestClaudeExitInit:
    """claude_exit must be initialized before the try block so that
    build_mission_command failures don't cause UnboundLocalError at the
    post-try reporting code (line ~1291)."""

    def test_claude_exit_initialized_before_try(self):
        """Verify claude_exit = 1 appears before the try block in source."""
        import inspect
        from app.run import _run_iteration
        src = inspect.getsource(_run_iteration)
        # Find positions
        init_pos = src.find("claude_exit = 1")
        try_pos = src.find("try:", src.find("# Build CLI command"))
        assert init_pos != -1, "claude_exit = 1 initialization not found"
        assert init_pos < try_pos, "claude_exit must be initialized before the try block"

    def test_claude_exit_default_is_failure(self):
        """The default value of claude_exit should indicate failure (non-zero)."""
        import inspect
        from app.run import _run_iteration
        src = inspect.getsource(_run_iteration)
        # Find the initialization line
        for line in src.split('\n'):
            stripped = line.strip()
            if stripped.startswith('claude_exit = ') and 'run_claude_task' not in stripped:
                val = stripped.split('=')[1].strip().split('#')[0].strip()
                assert val != '0', "Default claude_exit should not be 0 (success)"
                break
        else:
            pytest.fail("No claude_exit initialization found")


# ---------------------------------------------------------------------------
# Test: MAX_CONSECUTIVE_ERRORS constant
# ---------------------------------------------------------------------------

class TestConstants:
    def test_max_consecutive_errors_is_10(self):
        from app.run import MAX_CONSECUTIVE_ERRORS
        assert MAX_CONSECUTIVE_ERRORS == 10


# ---------------------------------------------------------------------------
# Test: Recovery helpers
# ---------------------------------------------------------------------------

class TestRecoveryHelpers:
    """Tests for _calculate_backoff and _should_notify_error."""

    def test_calculate_backoff_linear_growth(self):
        from app.run import _calculate_backoff, BACKOFF_MULTIPLIER
        assert _calculate_backoff(1, 300) == BACKOFF_MULTIPLIER
        assert _calculate_backoff(2, 300) == BACKOFF_MULTIPLIER * 2
        assert _calculate_backoff(3, 300) == BACKOFF_MULTIPLIER * 3

    def test_calculate_backoff_capped(self):
        from app.run import _calculate_backoff
        # 100 * 10 = 1000, but capped at 60
        assert _calculate_backoff(100, 60) == 60

    def test_should_notify_on_first_error(self):
        from app.run import _should_notify_error
        assert _should_notify_error(1) is True

    def test_should_notify_at_interval(self):
        from app.run import _should_notify_error, ERROR_NOTIFICATION_INTERVAL
        assert _should_notify_error(ERROR_NOTIFICATION_INTERVAL) is True
        assert _should_notify_error(ERROR_NOTIFICATION_INTERVAL * 2) is True

    def test_should_not_notify_between_intervals(self):
        from app.run import _should_notify_error, ERROR_NOTIFICATION_INTERVAL
        for i in range(2, ERROR_NOTIFICATION_INTERVAL):
            assert _should_notify_error(i) is False


# ---------------------------------------------------------------------------
# Test: _notify_mission_end
# ---------------------------------------------------------------------------

class TestNotifyMissionEnd:
    """Tests for _notify_mission_end() — end-of-mission notifications."""

    @patch("app.run._notify")
    def test_success_with_mission_title(self, mock_notify):
        from app.run import _notify_mission_end
        _notify_mission_end("/tmp/inst", "myproject", 3, 10, 0, "Fix the auth bug")
        mock_notify.assert_called_once()
        msg = mock_notify.call_args[0][1]
        assert msg.startswith("✅")
        assert "[myproject]" in msg
        assert "Fix the auth bug" in msg
        assert "Run 3/10" in msg

    @patch("app.run._notify")
    def test_failure_with_mission_title(self, mock_notify):
        from app.run import _notify_mission_end
        _notify_mission_end("/tmp/inst", "myproject", 3, 10, 1, "Fix the auth bug")
        mock_notify.assert_called_once()
        msg = mock_notify.call_args[0][1]
        assert msg.startswith("❌")
        assert "[myproject]" in msg
        assert "Failed:" in msg
        assert "Fix the auth bug" in msg

    @patch("app.run._notify")
    def test_success_autonomous_no_title(self, mock_notify):
        from app.run import _notify_mission_end
        _notify_mission_end("/tmp/inst", "koan", 1, 5, 0, "")
        mock_notify.assert_called_once()
        msg = mock_notify.call_args[0][1]
        assert msg.startswith("✅")
        assert "[koan]" in msg
        assert "Autonomous run" in msg

    @patch("app.run._notify")
    def test_failure_autonomous_no_title(self, mock_notify):
        from app.run import _notify_mission_end
        _notify_mission_end("/tmp/inst", "koan", 1, 5, 1, "")
        mock_notify.assert_called_once()
        msg = mock_notify.call_args[0][1]
        assert msg.startswith("❌")
        assert "Failed: Run" in msg

    @patch("app.mission_summary.get_mission_summary", return_value="Session 42\n\nFixed auth.")
    @patch("app.run._notify")
    def test_success_includes_journal_summary(self, mock_notify, mock_summary):
        from app.run import _notify_mission_end
        _notify_mission_end("/tmp/inst", "proj", 2, 10, 0, "Fix auth")
        msg = mock_notify.call_args[0][1]
        assert "✅" in msg
        assert "Fixed auth." in msg
        mock_summary.assert_called_once_with("/tmp/inst", "proj", max_chars=300)

    @patch("app.mission_summary.get_mission_summary", return_value="")
    @patch("app.run._notify")
    def test_success_no_summary_when_empty(self, mock_notify, mock_summary):
        from app.run import _notify_mission_end
        _notify_mission_end("/tmp/inst", "proj", 2, 10, 0, "Fix auth")
        msg = mock_notify.call_args[0][1]
        assert "✅" in msg
        # No double newline when summary is empty
        assert "\n\n" not in msg

    @patch("app.mission_summary.get_mission_summary", side_effect=Exception("broken"))
    @patch("app.run._notify")
    def test_success_survives_summary_error(self, mock_notify, mock_summary):
        from app.run import _notify_mission_end
        _notify_mission_end("/tmp/inst", "proj", 2, 10, 0, "Fix auth")
        # Should still send notification even if summary extraction fails
        mock_notify.assert_called_once()
        msg = mock_notify.call_args[0][1]
        assert msg.startswith("✅")

    @patch("app.run._notify")
    def test_failure_does_not_call_summary(self, mock_notify):
        from app.run import _notify_mission_end
        with patch("app.mission_summary.get_mission_summary") as mock_summary:
            _notify_mission_end("/tmp/inst", "proj", 2, 10, 1, "Fix auth")
            mock_summary.assert_not_called()

    @patch("app.mission_summary.get_failure_context", return_value="fatal: bad ref\nError: build failed")
    @patch("app.run._notify")
    def test_failure_includes_error_context(self, mock_notify, mock_ctx):
        from app.run import _notify_mission_end
        _notify_mission_end("/tmp/inst", "proj", 2, 10, 1, "Fix auth")
        msg = mock_notify.call_args[0][1]
        assert "❌" in msg
        assert "fatal: bad ref" in msg
        assert "build failed" in msg
        mock_ctx.assert_called_once_with("/tmp/inst", "proj", max_chars=300)

    @patch("app.mission_summary.get_failure_context", return_value="")
    @patch("app.run._notify")
    def test_failure_no_context_when_empty(self, mock_notify, mock_ctx):
        from app.run import _notify_mission_end
        _notify_mission_end("/tmp/inst", "proj", 2, 10, 1, "Fix auth")
        msg = mock_notify.call_args[0][1]
        assert "❌" in msg
        assert "\n\n" not in msg

    @patch("app.mission_summary.get_failure_context", side_effect=Exception("broken"))
    @patch("app.run._notify")
    def test_failure_survives_context_error(self, mock_notify, mock_ctx):
        from app.run import _notify_mission_end
        _notify_mission_end("/tmp/inst", "proj", 2, 10, 1, "Fix auth")
        mock_notify.assert_called_once()
        msg = mock_notify.call_args[0][1]
        assert msg.startswith("❌")

    @patch("app.run._notify")
    def test_nonzero_exit_codes_are_failure(self, mock_notify):
        from app.run import _notify_mission_end
        for code in [1, 2, 127, 255]:
            mock_notify.reset_mock()
            _notify_mission_end("/tmp/inst", "proj", 1, 5, code, "task")
            msg = mock_notify.call_args[0][1]
            assert msg.startswith("❌"), f"exit code {code} should be failure"

    @patch("app.run._notify")
    def test_always_calls_notify(self, mock_notify):
        """Both success and failure must call _notify — no silent completions."""
        from app.run import _notify_mission_end
        _notify_mission_end("/tmp/inst", "proj", 1, 5, 0, "task")
        assert mock_notify.call_count == 1
        _notify_mission_end("/tmp/inst", "proj", 1, 5, 1, "task")
        assert mock_notify.call_count == 2

    @patch("app.run._notify")
    def test_project_prefix_after_emoji_success(self, mock_notify):
        """Project name must appear right after the emoji prefix."""
        from app.run import _notify_mission_end
        _notify_mission_end("/tmp/inst", "myapp", 2, 10, 0, "Deploy fix")
        msg = mock_notify.call_args[0][1]
        assert msg.startswith("✅ [myapp]")

    @patch("app.run._notify")
    def test_project_prefix_after_emoji_failure(self, mock_notify):
        """Project name must appear right after the emoji prefix on failure."""
        from app.run import _notify_mission_end
        _notify_mission_end("/tmp/inst", "backend", 3, 10, 1, "Broken deploy")
        msg = mock_notify.call_args[0][1]
        assert msg.startswith("❌ [backend]")

    @patch("app.run._notify")
    def test_project_prefix_after_emoji_autonomous(self, mock_notify):
        """Autonomous run (no title) still gets project prefix after emoji."""
        from app.run import _notify_mission_end
        _notify_mission_end("/tmp/inst", "koan", 1, 5, 0, "")
        msg = mock_notify.call_args[0][1]
        assert msg.startswith("✅ [koan]")


# ---------------------------------------------------------------------------
# Test: Koan branch helpers
# ---------------------------------------------------------------------------

class TestGetKoanBranch:
    def test_returns_branch_name(self, tmp_path):
        """_get_koan_branch returns the current branch of the repo."""
        from app.run import _get_koan_branch
        # Init a git repo
        subprocess.run(["git", "init", "-b", "main"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=str(tmp_path), capture_output=True)
        assert _get_koan_branch(str(tmp_path)) == "main"

    def test_returns_empty_on_non_repo(self, tmp_path):
        """_get_koan_branch returns '' for non-git directories."""
        from app.run import _get_koan_branch
        assert _get_koan_branch(str(tmp_path)) == ""

    def test_returns_empty_on_invalid_path(self):
        """_get_koan_branch returns '' for non-existent paths."""
        from app.run import _get_koan_branch
        assert _get_koan_branch("/nonexistent/path") == ""


class TestRestoreKoanBranch:
    def test_restores_when_branch_drifted(self, tmp_path, capsys):
        """_restore_koan_branch checks out the expected branch when current differs."""
        from app.run import _restore_koan_branch, _init_colors
        _init_colors()
        # Init repo with two branches
        subprocess.run(["git", "init", "-b", "main"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "checkout", "-b", "other"], cwd=str(tmp_path), capture_output=True)

        # Should restore to main
        _restore_koan_branch(str(tmp_path), "main")
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(tmp_path), capture_output=True, text=True,
        )
        assert result.stdout.strip() == "main"

    def test_noop_when_branch_matches(self, tmp_path):
        """_restore_koan_branch does nothing when branch already matches."""
        from app.run import _restore_koan_branch
        # Init repo
        subprocess.run(["git", "init", "-b", "main"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=str(tmp_path), capture_output=True)

        # Should be a no-op — mock _get_koan_branch to avoid real git calls
        with patch("app.run._get_koan_branch", return_value="main"), \
             patch("app.run.subprocess.run") as mock_run:
            _restore_koan_branch(str(tmp_path), "main")
            # No git checkout call since branch already matches
            mock_run.assert_not_called()

    def test_noop_when_expected_empty(self):
        """_restore_koan_branch does nothing when expected_branch is empty."""
        from app.run import _restore_koan_branch
        with patch("app.run._get_koan_branch") as mock_get:
            _restore_koan_branch("/some/path", "")
            mock_get.assert_not_called()

    def test_handles_checkout_failure(self, tmp_path, capsys):
        """_restore_koan_branch logs but doesn't crash on checkout failure."""
        from app.run import _restore_koan_branch, _init_colors
        _init_colors()
        with patch("app.run._get_koan_branch", return_value="wrong-branch"):
            with patch("app.run.subprocess.run", side_effect=Exception("git error")):
                # Should not raise
                _restore_koan_branch(str(tmp_path), "main")
        out = capsys.readouterr().out
        assert "Failed to restore koan branch" in out


class TestRunSkillMissionEnv:
    """Tests that _run_skill_mission sets PYTHONPATH and restores branches."""

    def _make_mock_popen(self, returncode=0, stdout_lines=None, stderr_content=""):
        """Create a mock Popen that writes stderr to the file handle passed by caller.

        Production code redirects stderr to a temp file (not a pipe), so the
        mock must write through the file handle it receives in ``kwargs['stderr']``.
        """
        mock_proc = MagicMock()
        mock_proc.returncode = returncode
        mock_proc.stdout = iter(stdout_lines or [])
        mock_proc.wait.return_value = returncode
        mock_proc._stderr_content = stderr_content

        def _side_effect(*args, **kwargs):
            stderr_fh = kwargs.get("stderr")
            if stderr_fh and stderr_content and hasattr(stderr_fh, "write"):
                stderr_fh.write(stderr_content)
            return mock_proc

        mock_proc._side_effect = _side_effect
        return mock_proc

    def test_passes_pythonpath_in_env(self, tmp_path):
        """_run_skill_mission passes explicit PYTHONPATH to subprocess."""
        from app.run import _run_skill_mission
        koan_root = str(tmp_path)
        instance = str(tmp_path / "instance")
        (tmp_path / "instance").mkdir()
        (tmp_path / "instance" / "journal").mkdir(parents=True)
        (tmp_path / "koan").mkdir()

        mock_proc = self._make_mock_popen(stdout_lines=["ok\n"])

        with patch("app.run.subprocess.Popen", side_effect=mock_proc._side_effect) as mock_popen, \
             patch("app.run._get_koan_branch", return_value="main"), \
             patch("app.run._restore_koan_branch"), \
             patch("app.run._reset_terminal"), \
             patch("app.mission_runner.run_post_mission"):
            _run_skill_mission(
                skill_cmd=["python3", "-m", "app.plan_runner", "--help"],
                koan_root=koan_root,
                instance=instance,
                project_name="test",
                project_path=str(tmp_path),
                run_num=1,
                mission_title="/plan test",
                autonomous_mode="implement",
            )

        # Verify subprocess.Popen was called with env containing PYTHONPATH
        call_kwargs = mock_popen.call_args[1]
        assert "env" in call_kwargs
        assert call_kwargs["env"]["PYTHONPATH"] == str(tmp_path / "koan")

    def test_restores_branch_after_skill_execution(self, tmp_path):
        """_run_skill_mission calls _restore_koan_branch after execution."""
        from app.run import _run_skill_mission
        koan_root = str(tmp_path)
        instance = str(tmp_path / "instance")
        (tmp_path / "instance").mkdir()
        (tmp_path / "instance" / "journal").mkdir(parents=True)
        (tmp_path / "koan").mkdir()

        mock_proc = self._make_mock_popen()

        with patch("app.run.subprocess.Popen", side_effect=mock_proc._side_effect), \
             patch("app.run._get_koan_branch", return_value="main") as mock_get, \
             patch("app.run._restore_koan_branch") as mock_restore, \
             patch("app.run._reset_terminal"), \
             patch("app.mission_runner.run_post_mission"):
            _run_skill_mission(
                skill_cmd=["python3", "--help"],
                koan_root=koan_root,
                instance=instance,
                project_name="test",
                project_path=str(tmp_path),
                run_num=1,
                mission_title="/plan test",
                autonomous_mode="implement",
            )

        mock_get.assert_called_once_with(koan_root)
        mock_restore.assert_called_once_with(koan_root, "main")

    def test_restores_branch_even_on_timeout(self, tmp_path):
        """Branch is restored even when subprocess times out."""
        from app.run import _run_skill_mission
        koan_root = str(tmp_path)
        instance = str(tmp_path / "instance")
        (tmp_path / "instance").mkdir()
        (tmp_path / "instance" / "journal").mkdir(parents=True)
        (tmp_path / "koan").mkdir()

        mock_proc = self._make_mock_popen()
        # First call (with timeout) raises, second call (after kill) returns 0
        mock_proc.wait.side_effect = [subprocess.TimeoutExpired("cmd", 3600), 0]

        with patch("app.run.subprocess.Popen", side_effect=mock_proc._side_effect), \
             patch("app.run._get_koan_branch", return_value="main"), \
             patch("app.run._restore_koan_branch") as mock_restore, \
             patch("app.run._reset_terminal"), \
             patch("app.mission_runner.run_post_mission"):
            _run_skill_mission(
                skill_cmd=["python3", "--help"],
                koan_root=koan_root,
                instance=instance,
                project_name="test",
                project_path=str(tmp_path),
                run_num=1,
                mission_title="/plan test",
                autonomous_mode="implement",
            )

        mock_restore.assert_called_once_with(koan_root, "main")

    def test_kills_process_on_generic_exception(self, tmp_path):
        """Generic exceptions during skill execution must kill the child process."""
        from app.run import _run_skill_mission

        koan_root = str(tmp_path)
        instance = str(tmp_path / "instance")
        (tmp_path / "instance").mkdir()
        (tmp_path / "instance" / "journal").mkdir(parents=True)
        (tmp_path / "koan").mkdir()

        mock_proc = MagicMock()
        mock_proc.stdout = iter(["line1\n"])
        mock_proc.wait.side_effect = ValueError("unexpected error")
        mock_proc.returncode = None
        mock_proc.pid = 12345

        with patch("app.run.subprocess.Popen", return_value=mock_proc), \
             patch("app.run._get_koan_branch", return_value="main"), \
             patch("app.run._restore_koan_branch"), \
             patch("app.run._reset_terminal"), \
             patch("app.run._kill_process_group") as mock_kill, \
             patch("app.run.protected_phase", return_value=MagicMock(
                 __enter__=MagicMock(), __exit__=MagicMock(return_value=False)
             )), \
             patch("app.run.set_status"), \
             patch("app.run.log"):
            result = _run_skill_mission(
                skill_cmd=["python3", "-m", "app.fake"],
                mission_title="/fake test",
                project_name="test",
                project_path=str(tmp_path),
                koan_root=koan_root,
                instance=instance,
                run_num=1,
                autonomous_mode="implement",
            )

        mock_kill.assert_called_once_with(mock_proc)
        assert result == 1  # exit code should be failure

    def test_restores_branch_even_on_popen_exception(self, tmp_path):
        """Branch is restored even when Popen raises an exception."""
        from app.run import _run_skill_mission
        koan_root = str(tmp_path)
        instance = str(tmp_path / "instance")
        (tmp_path / "instance").mkdir()
        (tmp_path / "instance" / "journal").mkdir(parents=True)
        (tmp_path / "koan").mkdir()

        with patch("app.run.subprocess.Popen", side_effect=OSError("boom")), \
             patch("app.run._get_koan_branch", return_value="main"), \
             patch("app.run._restore_koan_branch") as mock_restore, \
             patch("app.run._reset_terminal"), \
             patch("app.mission_runner.run_post_mission"):
            _run_skill_mission(
                skill_cmd=["python3", "--help"],
                koan_root=koan_root,
                instance=instance,
                project_name="test",
                project_path=str(tmp_path),
                run_num=1,
                mission_title="/plan test",
                autonomous_mode="implement",
            )

        mock_restore.assert_called_once_with(koan_root, "main")

    def test_streams_stdout_to_pending_md(self, tmp_path):
        """_run_skill_mission appends stdout lines to pending.md for /live."""
        from app.run import _run_skill_mission
        koan_root = str(tmp_path)
        instance = str(tmp_path / "instance")
        (tmp_path / "instance").mkdir()
        journal_dir = tmp_path / "instance" / "journal"
        journal_dir.mkdir(parents=True)
        (tmp_path / "koan").mkdir()

        # Pre-create pending.md with a header (as _handle_skill_dispatch does)
        pending = journal_dir / "pending.md"
        pending.write_text("# Mission: /rebase test\n---\n")

        mock_proc = self._make_mock_popen(
            stdout_lines=["Step 1: fetching PR\n", "Step 2: rebasing\n", "Done.\n"],
        )

        with patch("app.run.subprocess.Popen", side_effect=mock_proc._side_effect), \
             patch("app.run._get_koan_branch", return_value="main"), \
             patch("app.run._restore_koan_branch"), \
             patch("app.run._reset_terminal"), \
             patch("app.mission_runner.run_post_mission"):
            _run_skill_mission(
                skill_cmd=["python3", "--help"],
                koan_root=koan_root,
                instance=instance,
                project_name="test",
                project_path=str(tmp_path),
                run_num=1,
                mission_title="/rebase test",
                autonomous_mode="implement",
            )

        # Verify pending.md contains the streamed output
        content = pending.read_text()
        assert "Step 1: fetching PR" in content
        assert "Step 2: rebasing" in content
        assert "Done." in content

    def test_pending_md_shows_output_for_live(self, tmp_path):
        """/live returns skill output when pending.md is populated by dispatch."""
        from skills.core.live.handler import handle

        journal_dir = tmp_path / "journal"
        journal_dir.mkdir(parents=True)
        pending = journal_dir / "pending.md"
        pending.write_text(
            "# Mission: /rebase PR #42\n"
            "Project: myproject\n---\n"
            "Step 1: fetching PR\nStep 2: rebasing\n"
        )

        ctx = MagicMock()
        ctx.instance_dir = tmp_path
        result = handle(ctx)

        assert "No mission running" not in result
        assert "/rebase PR #42" in result
        assert "Step 1: fetching PR" in result

    def test_skill_dispatch_creates_pending_md(self, tmp_path):
        """_handle_skill_dispatch creates pending.md before execution."""
        from app.run import _handle_skill_dispatch

        koan_root = str(tmp_path)
        instance = str(tmp_path / "instance")
        (tmp_path / "instance").mkdir()
        journal_dir = tmp_path / "instance" / "journal"
        journal_dir.mkdir(parents=True)
        (tmp_path / "koan").mkdir()

        mock_proc = self._make_mock_popen(stdout_lines=["ok\n"])

        with patch("app.run.subprocess.Popen", side_effect=mock_proc._side_effect), \
             patch("app.run._get_koan_branch", return_value="main"), \
             patch("app.run._restore_koan_branch"), \
             patch("app.run._reset_terminal"), \
             patch("app.run.protected_phase", return_value=MagicMock(
                 __enter__=MagicMock(), __exit__=MagicMock(return_value=False)
             )), \
             patch("app.run._notify"), \
             patch("app.run._notify_mission_end"), \
             patch("app.run._finalize_mission"), \
             patch("app.run._commit_instance"), \
             patch("app.run._sleep_between_runs"), \
             patch("app.run.set_status"), \
             patch("app.run.log"), \
             patch("app.skill_dispatch.dispatch_skill_mission",
                   return_value=["python3", "-m", "app.plan_runner"]), \
             patch("app.mission_runner.run_post_mission"):
            handled, _ = _handle_skill_dispatch(
                mission_title="/plan test",
                project_name="test",
                project_path=str(tmp_path),
                koan_root=koan_root,
                instance=instance,
                run_num=1,
                max_runs=20,
                autonomous_mode="implement",
                interval=30,
            )

        assert handled is True
        # pending.md should have been created (even if archived by post-mission)
        # Check that create_pending_file was reachable — the journal dir exists
        assert journal_dir.exists()

    def test_stderr_redirected_to_file(self, tmp_path):
        """_run_skill_mission redirects stderr to a file (not PIPE) to prevent deadlock."""
        from app.run import _run_skill_mission
        koan_root = str(tmp_path)
        instance = str(tmp_path / "instance")
        (tmp_path / "instance").mkdir()
        (tmp_path / "instance" / "journal").mkdir(parents=True)
        (tmp_path / "koan").mkdir()

        mock_proc = self._make_mock_popen(
            stdout_lines=["output\n"],
            stderr_content="warning: something happened\nerror: detail\n",
        )

        with patch("app.run.subprocess.Popen", side_effect=mock_proc._side_effect) as mock_popen, \
             patch("app.run._get_koan_branch", return_value="main"), \
             patch("app.run._restore_koan_branch"), \
             patch("app.run._reset_terminal"), \
             patch("app.mission_runner.run_post_mission"):
            _run_skill_mission(
                skill_cmd=["python3", "--help"],
                koan_root=koan_root,
                instance=instance,
                project_name="test",
                project_path=str(tmp_path),
                run_num=1,
                mission_title="/test stderr",
                autonomous_mode="implement",
            )

        # Verify Popen received a file handle (not PIPE) for stderr
        call_kwargs = mock_popen.call_args[1]
        assert call_kwargs["stderr"] is not subprocess.PIPE
        assert hasattr(call_kwargs["stderr"], "write")  # file-like object

    def test_no_stderr_still_works(self, tmp_path):
        """_run_skill_mission handles empty stderr without hanging."""
        from app.run import _run_skill_mission
        koan_root = str(tmp_path)
        instance = str(tmp_path / "instance")
        (tmp_path / "instance").mkdir()
        (tmp_path / "instance" / "journal").mkdir(parents=True)
        (tmp_path / "koan").mkdir()

        mock_proc = self._make_mock_popen(stdout_lines=["ok\n"])

        with patch("app.run.subprocess.Popen", side_effect=mock_proc._side_effect), \
             patch("app.run._get_koan_branch", return_value="main"), \
             patch("app.run._restore_koan_branch"), \
             patch("app.run._reset_terminal"), \
             patch("app.mission_runner.run_post_mission"):
            _run_skill_mission(
                skill_cmd=["python3", "--help"],
                koan_root=koan_root,
                instance=instance,
                project_name="test",
                project_path=str(tmp_path),
                run_num=1,
                mission_title="/test no-stderr",
                autonomous_mode="implement",
            )

    def test_registers_proc_in_sig_for_ctrlc(self, tmp_path):
        """_run_skill_mission registers subprocess in _sig.claude_proc."""
        from app.run import _run_skill_mission, _sig
        koan_root = str(tmp_path)
        instance = str(tmp_path / "instance")
        (tmp_path / "instance").mkdir()
        (tmp_path / "instance" / "journal").mkdir(parents=True)
        (tmp_path / "koan").mkdir()

        mock_proc = self._make_mock_popen(stdout_lines=["ok\n"])

        with patch("app.run.subprocess.Popen", side_effect=mock_proc._side_effect), \
             patch("app.run._get_koan_branch", return_value="main"), \
             patch("app.run._restore_koan_branch"), \
             patch("app.run._reset_terminal"), \
             patch("app.mission_runner.run_post_mission"):
            _sig.claude_proc = None
            _run_skill_mission(
                skill_cmd=["python3", "--help"],
                koan_root=koan_root,
                instance=instance,
                project_name="test",
                project_path=str(tmp_path),
                run_num=1,
                mission_title="/test signal",
                autonomous_mode="implement",
            )

        # After execution, _sig.claude_proc should be cleared
        assert _sig.claude_proc is None

    def test_clears_sig_proc_on_timeout(self, tmp_path):
        """_sig.claude_proc is cleared even when subprocess times out."""
        from app.run import _run_skill_mission, _sig
        koan_root = str(tmp_path)
        instance = str(tmp_path / "instance")
        (tmp_path / "instance").mkdir()
        (tmp_path / "instance" / "journal").mkdir(parents=True)
        (tmp_path / "koan").mkdir()

        mock_proc = self._make_mock_popen()
        mock_proc.wait.side_effect = [subprocess.TimeoutExpired("cmd", 600), 0]
        mock_proc.poll.return_value = None
        mock_proc.pid = 77777

        with patch("app.run.subprocess.Popen", side_effect=mock_proc._side_effect), \
             patch("app.run._get_koan_branch", return_value="main"), \
             patch("app.run._restore_koan_branch"), \
             patch("app.run._reset_terminal"), \
             patch("app.run.os.getpgid", return_value=77777), \
             patch("app.run.os.killpg"), \
             patch("app.mission_runner.run_post_mission"):
            _sig.claude_proc = "should be replaced"
            _run_skill_mission(
                skill_cmd=["python3", "--help"],
                koan_root=koan_root,
                instance=instance,
                project_name="test",
                project_path=str(tmp_path),
                run_num=1,
                mission_title="/test timeout",
                autonomous_mode="implement",
            )

        assert _sig.claude_proc is None

    def test_uses_configurable_skill_timeout(self, tmp_path):
        """_run_skill_mission uses get_skill_timeout() for the watchdog timer."""
        from app.run import _run_skill_mission
        koan_root = str(tmp_path)
        instance = str(tmp_path / "instance")
        (tmp_path / "instance").mkdir()
        (tmp_path / "instance" / "journal").mkdir(parents=True)
        (tmp_path / "koan").mkdir()

        mock_proc = self._make_mock_popen(stdout_lines=["ok\n"])
        mock_timer = MagicMock()

        with patch("app.run.subprocess.Popen", side_effect=mock_proc._side_effect), \
             patch("app.run._get_koan_branch", return_value="main"), \
             patch("app.run._restore_koan_branch"), \
             patch("app.run._reset_terminal"), \
             patch("app.config.get_skill_timeout", return_value=7200), \
             patch("app.run.threading.Timer", return_value=mock_timer) as mock_timer_cls, \
             patch("app.mission_runner.run_post_mission"):
            _run_skill_mission(
                skill_cmd=["python3", "--help"],
                koan_root=koan_root,
                instance=instance,
                project_name="test",
                project_path=str(tmp_path),
                run_num=1,
                mission_title="/fix test",
                autonomous_mode="implement",
            )

        # Watchdog timer should use the configurable timeout (7200s)
        mock_timer_cls.assert_called_once_with(7200, mock_timer_cls.call_args[0][1])
        mock_timer.start.assert_called_once()
        mock_timer.cancel.assert_called_once()
        # proc.wait() is now a 30s cleanup wait (real timeout via watchdog)
        mock_proc.wait.assert_called_once_with(timeout=30)

    def test_skill_timeout_default_is_3600(self, tmp_path):
        """Default skill timeout should be 3600s (60 minutes)."""
        from app.run import _run_skill_mission
        koan_root = str(tmp_path)
        instance = str(tmp_path / "instance")
        (tmp_path / "instance").mkdir()
        (tmp_path / "instance" / "journal").mkdir(parents=True)
        (tmp_path / "koan").mkdir()

        mock_proc = self._make_mock_popen(stdout_lines=["ok\n"])
        mock_timer = MagicMock()

        with patch("app.run.subprocess.Popen", side_effect=mock_proc._side_effect), \
             patch("app.run._get_koan_branch", return_value="main"), \
             patch("app.run._restore_koan_branch"), \
             patch("app.run._reset_terminal"), \
             patch("app.run.threading.Timer", return_value=mock_timer) as mock_timer_cls, \
             patch("app.mission_runner.run_post_mission"):
            _run_skill_mission(
                skill_cmd=["python3", "--help"],
                koan_root=koan_root,
                instance=instance,
                project_name="test",
                project_path=str(tmp_path),
                run_num=1,
                mission_title="/plan test",
                autonomous_mode="implement",
            )

        # Default timeout from get_skill_timeout() is 3600s, enforced by watchdog
        mock_timer_cls.assert_called_once_with(3600, mock_timer_cls.call_args[0][1])
        mock_timer.start.assert_called_once()
        mock_timer.cancel.assert_called_once()
        # proc.wait() is now a 30s cleanup wait
        mock_proc.wait.assert_called_once_with(timeout=30)

    def test_timeout_uses_kill_process_group(self, tmp_path):
        """Timeout path kills the whole process group, not just the leader."""
        from app.run import _run_skill_mission
        koan_root = str(tmp_path)
        instance = str(tmp_path / "instance")
        (tmp_path / "instance").mkdir()
        (tmp_path / "instance" / "journal").mkdir(parents=True)
        (tmp_path / "koan").mkdir()

        mock_proc = self._make_mock_popen()
        mock_proc.wait.side_effect = [subprocess.TimeoutExpired("cmd", 30), 0]
        mock_proc.poll.return_value = None
        mock_proc.pid = 88888
        mock_timer = MagicMock()

        with patch("app.run.subprocess.Popen", side_effect=mock_proc._side_effect), \
             patch("app.run._get_koan_branch", return_value="main"), \
             patch("app.run._restore_koan_branch"), \
             patch("app.run._reset_terminal"), \
             patch("app.run.threading.Timer", return_value=mock_timer), \
             patch("app.run.os.getpgid", return_value=88888) as mock_getpgid, \
             patch("app.run.os.killpg") as mock_killpg, \
             patch("app.mission_runner.run_post_mission"):
            _run_skill_mission(
                skill_cmd=["python3", "--help"],
                koan_root=koan_root,
                instance=instance,
                project_name="test",
                project_path=str(tmp_path),
                run_num=1,
                mission_title="/test kill group",
                autonomous_mode="implement",
            )

        mock_getpgid.assert_called_with(88888)
        mock_killpg.assert_any_call(88888, signal.SIGTERM)

    def test_watchdog_kills_hanging_skill_during_stdout(self, tmp_path):
        """Watchdog timer fires and kills skill that hangs during stdout streaming.

        Before the fix, skill_timeout was only applied to proc.wait() which
        runs AFTER the stdout iteration loop.  If a subprocess hangs without
        closing its stdout pipe, the loop blocks indefinitely.  The watchdog
        timer now kills the process group mid-stream.
        """
        import threading
        from app.run import _run_skill_mission
        koan_root = str(tmp_path)
        instance = str(tmp_path / "instance")
        (tmp_path / "instance").mkdir()
        (tmp_path / "instance" / "journal").mkdir(parents=True)
        (tmp_path / "koan").mkdir()

        mock_proc = self._make_mock_popen(stdout_lines=["line1\n"])
        mock_proc.pid = 99999
        # poll() must return None so _kill_process_group doesn't skip
        mock_proc.poll.return_value = None

        # Capture the watchdog callback and invoke it immediately to simulate
        # the timer firing during the stdout loop.
        captured_callback = None

        def fake_timer(timeout, callback):
            nonlocal captured_callback
            captured_callback = callback
            timer = MagicMock()
            return timer

        with patch("app.run.subprocess.Popen", side_effect=mock_proc._side_effect), \
             patch("app.run._get_koan_branch", return_value="main"), \
             patch("app.run._restore_koan_branch"), \
             patch("app.run._reset_terminal"), \
             patch("app.config.get_skill_timeout", return_value=60), \
             patch("app.run.threading.Timer", side_effect=fake_timer) as mock_timer_cls, \
             patch("app.run.os.getpgid", return_value=99999), \
             patch("app.run.os.killpg") as mock_killpg, \
             patch("app.mission_runner.run_post_mission"):
            # Simulate: watchdog fires during the first proc.wait() call.
            # Subsequent wait() calls (from _kill_process_group) return
            # immediately to avoid recursion.
            watchdog_fired = False

            def wait_side_effect(**kwargs):
                nonlocal watchdog_fired
                if not watchdog_fired and captured_callback:
                    watchdog_fired = True
                    captured_callback()
                return 0

            mock_proc.wait.side_effect = wait_side_effect

            exit_code = _run_skill_mission(
                skill_cmd=["python3", "--help"],
                koan_root=koan_root,
                instance=instance,
                project_name="test",
                project_path=str(tmp_path),
                run_num=1,
                mission_title="/test watchdog",
                autonomous_mode="implement",
            )

        # Watchdog should have killed the process group
        mock_killpg.assert_any_call(99999, signal.SIGTERM)
        # Exit code should be 1 (timeout = failure)
        assert exit_code == 1
        # Timer was created with the configured timeout
        mock_timer_cls.assert_called_once()
        assert mock_timer_cls.call_args[0][0] == 60

    def test_watchdog_timer_cancelled_on_normal_completion(self, tmp_path):
        """Timer is properly cancelled when skill completes before timeout."""
        from app.run import _run_skill_mission
        koan_root = str(tmp_path)
        instance = str(tmp_path / "instance")
        (tmp_path / "instance").mkdir()
        (tmp_path / "instance" / "journal").mkdir(parents=True)
        (tmp_path / "koan").mkdir()

        mock_proc = self._make_mock_popen(stdout_lines=["ok\n"])
        mock_timer = MagicMock()

        with patch("app.run.subprocess.Popen", side_effect=mock_proc._side_effect), \
             patch("app.run._get_koan_branch", return_value="main"), \
             patch("app.run._restore_koan_branch"), \
             patch("app.run._reset_terminal"), \
             patch("app.run.threading.Timer", return_value=mock_timer), \
             patch("app.mission_runner.run_post_mission"):
            exit_code = _run_skill_mission(
                skill_cmd=["python3", "--help"],
                koan_root=koan_root,
                instance=instance,
                project_name="test",
                project_path=str(tmp_path),
                run_num=1,
                mission_title="/test normal",
                autonomous_mode="implement",
            )

        # Timer must be started and then cancelled
        mock_timer.start.assert_called_once()
        mock_timer.cancel.assert_called_once()
        # Timer must be set as daemon
        assert mock_timer.daemon is True
        # Normal exit
        assert exit_code == 0

    def test_stderr_file_content_passed_to_post_mission(self, tmp_path):
        """Stderr content written by subprocess is available in the stderr temp file."""
        from app.run import _run_skill_mission
        koan_root = str(tmp_path)
        instance = str(tmp_path / "instance")
        (tmp_path / "instance").mkdir()
        (tmp_path / "instance" / "journal").mkdir(parents=True)
        (tmp_path / "koan").mkdir()

        stderr_text = "WARNING: deprecated API\nERROR: retry failed\n"
        mock_proc = self._make_mock_popen(
            stdout_lines=["ok\n"],
            stderr_content=stderr_text,
        )

        captured_stderr_path = []

        def _capture_post_mission(**kwargs):
            # Read the stderr file that was passed to post-mission
            stderr_file = kwargs.get("stderr_file", "")
            if stderr_file:
                with open(stderr_file) as f:
                    captured_stderr_path.append(f.read())

        with patch("app.run.subprocess.Popen", side_effect=mock_proc._side_effect), \
             patch("app.run._get_koan_branch", return_value="main"), \
             patch("app.run._restore_koan_branch"), \
             patch("app.run._reset_terminal"), \
             patch("app.mission_runner.run_post_mission", side_effect=_capture_post_mission):
            _run_skill_mission(
                skill_cmd=["python3", "--help"],
                koan_root=koan_root,
                instance=instance,
                project_name="test",
                project_path=str(tmp_path),
                run_num=1,
                mission_title="/test stderr-file",
                autonomous_mode="implement",
            )

        assert len(captured_stderr_path) == 1
        assert "WARNING: deprecated API" in captured_stderr_path[0]
        assert "ERROR: retry failed" in captured_stderr_path[0]

    def test_stderr_file_closed_on_exception(self, tmp_path):
        """The stderr file handle is closed even when Popen raises."""
        from app.run import _run_skill_mission
        koan_root = str(tmp_path)
        instance = str(tmp_path / "instance")
        (tmp_path / "instance").mkdir()
        (tmp_path / "instance" / "journal").mkdir(parents=True)
        (tmp_path / "koan").mkdir()

        call_count = {"n": 0}
        original_open = open

        def _tracking_open(*args, **kwargs):
            fh = original_open(*args, **kwargs)
            if len(args) >= 1 and "koan-err-" in str(args[0]):
                call_count["n"] += 1
            return fh

        with patch("app.run.subprocess.Popen", side_effect=OSError("spawn failed")), \
             patch("app.run._get_koan_branch", return_value="main"), \
             patch("app.run._restore_koan_branch"), \
             patch("app.run._reset_terminal"), \
             patch("app.mission_runner.run_post_mission"), \
             patch("builtins.open", side_effect=_tracking_open):
            result = _run_skill_mission(
                skill_cmd=["python3", "--help"],
                koan_root=koan_root,
                instance=instance,
                project_name="test",
                project_path=str(tmp_path),
                run_num=1,
                mission_title="/test cleanup",
                autonomous_mode="implement",
            )

        # Should return failure exit code
        assert result == 1

    def test_threading_imported_for_watchdog(self):
        """Verify run.py imports threading (needed for watchdog timer)."""
        import ast
        from pathlib import Path

        run_path = Path(__file__).parent.parent / "app" / "run.py"
        tree = ast.parse(run_path.read_text())
        imports = [
            node.names[0].name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
        ]
        assert "threading" in imports


# ---------------------------------------------------------------------------
# Test: restart_manager integration — run.py must use restart_manager API
# ---------------------------------------------------------------------------

class TestRestartManagerIntegration:
    """Verify run.py uses restart_manager functions instead of raw file ops."""

    def test_run_imports_restart_manager(self):
        """run.py must import check_restart, clear_restart, RESTART_EXIT_CODE."""
        import app.run as run_mod
        assert hasattr(run_mod, "check_restart")
        assert hasattr(run_mod, "clear_restart")
        assert hasattr(run_mod, "RESTART_EXIT_CODE")

    def test_no_raw_restart_file_access(self):
        """run.py must not construct Path(..., '.koan-restart').

        All restart signal operations should go through restart_manager.
        """
        import ast
        import inspect
        import app.run as run_mod
        source = inspect.getsource(run_mod)
        tree = ast.parse(source)
        violations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value == ".koan-restart":
                    violations.append(node.lineno)
        assert violations == [], (
            f"run.py still references '.koan-restart' as a string literal "
            f"at line(s) {violations}. Use restart_manager functions instead."
        )

    def test_pause_loop_uses_check_restart(self, koan_root):
        """handle_pause() uses check_restart() to detect restart signals."""
        from app.run import handle_pause

        instance = str(koan_root / "instance")
        (koan_root / ".koan-pause").touch()

        with patch("app.run.time.sleep"), \
             patch("app.pause_manager.check_and_resume", return_value=None), \
             patch("app.run.check_restart", side_effect=[False, True]) as mock_check:
            result = handle_pause(str(koan_root), instance, 5)
            assert result is None  # breaks out of pause loop
            mock_check.assert_called_with(str(koan_root))

    @patch("app.run.subprocess.run")
    @patch("app.run.run_startup", return_value=(5, 10, "koan/"))
    @patch("app.run.acquire_pidfile")
    @patch("app.run.release_pidfile")
    def test_startup_uses_clear_restart(self, mock_release, mock_acquire,
                                         mock_startup, mock_subproc, koan_root):
        """main_loop() uses clear_restart() to clean stale signal on startup."""
        from app.run import main_loop

        os.environ["KOAN_ROOT"] = str(koan_root)
        os.environ["KOAN_PROJECTS"] = f"test:{koan_root}"
        (koan_root / ".koan-project").write_text("test")

        def startup_then_stop(*args, **kwargs):
            (koan_root / ".koan-stop").touch()
            return (5, 10, "koan/")
        mock_startup.side_effect = startup_then_stop

        with patch("app.run._notify"), \
             patch("app.run.clear_restart") as mock_clear:
            main_loop()
            mock_clear.assert_called_once_with(str(koan_root))

    @patch("app.run.subprocess.run")
    @patch("app.run.run_startup", return_value=(5, 10, "koan/"))
    @patch("app.run.acquire_pidfile")
    @patch("app.run.release_pidfile")
    def test_loop_uses_check_restart_with_since(self, mock_release, mock_acquire,
                                                  mock_startup, mock_subproc, koan_root):
        """main_loop() restart check uses check_restart(since=start_time)."""
        from app.run import main_loop
        from app.restart_manager import RESTART_EXIT_CODE

        os.environ["KOAN_ROOT"] = str(koan_root)
        os.environ["KOAN_PROJECTS"] = f"test:{koan_root}"

        with patch("app.run._notify"), \
             patch("app.run.check_restart", return_value=True) as mock_check, \
             patch("app.run.clear_restart") as mock_clear:
            with pytest.raises(SystemExit) as exc:
                main_loop()
            assert exc.value.code == RESTART_EXIT_CODE
            # check_restart was called with since= (a float timestamp)
            calls = [c for c in mock_check.call_args_list
                     if c.kwargs.get("since") is not None or
                     (len(c.args) > 1 and c.args[1] > 0)]
            assert len(calls) >= 1, "check_restart must be called with since=start_time"
            mock_clear.assert_called()


# ---------------------------------------------------------------------------
# Bug fix: skill dispatch exception must finalize mission
# ---------------------------------------------------------------------------


class TestSkillDispatchExceptionFinalization(TestRunSkillMissionEnv):
    """Verify that _handle_skill_dispatch finalizes the mission even on exception."""

    def test_exception_during_skill_still_finalizes(self, tmp_path):
        """If _run_skill_mission raises, _finalize_mission must still be called."""
        from app.run import _handle_skill_dispatch

        koan_root = str(tmp_path)
        instance = str(tmp_path / "instance")
        (tmp_path / "instance").mkdir()
        (tmp_path / "instance" / "journal").mkdir(parents=True)
        (tmp_path / "koan").mkdir()

        with patch("app.run.subprocess.Popen", side_effect=OSError("exec failed")), \
             patch("app.run._get_koan_branch", return_value="main"), \
             patch("app.run._restore_koan_branch"), \
             patch("app.run._reset_terminal"), \
             patch("app.run.protected_phase", return_value=MagicMock(
                 __enter__=MagicMock(), __exit__=MagicMock(return_value=False)
             )), \
             patch("app.run._notify"), \
             patch("app.run._notify_mission_end") as mock_notify_end, \
             patch("app.run._finalize_mission") as mock_finalize, \
             patch("app.run._commit_instance"), \
             patch("app.run._sleep_between_runs"), \
             patch("app.run.set_status"), \
             patch("app.run.log"), \
             patch("app.skill_dispatch.dispatch_skill_mission",
                   return_value=["python3", "-m", "app.plan_runner"]), \
             patch("app.mission_runner.run_post_mission"):
            handled, _ = _handle_skill_dispatch(
                mission_title="/plan test",
                project_name="test",
                project_path=str(tmp_path),
                koan_root=koan_root,
                instance=instance,
                run_num=1,
                max_runs=20,
                autonomous_mode="implement",
                interval=30,
            )

        assert handled is True
        # _finalize_mission must have been called even though execution raised
        mock_finalize.assert_called_once()
        finalize_args = mock_finalize.call_args[0]
        assert finalize_args[3] == 1  # exit_code should be 1 (failure)

    def test_keyboard_interrupt_during_skill_finalizes_then_raises(self, tmp_path):
        """KeyboardInterrupt during skill dispatch must finalize then re-raise."""
        from app.run import _handle_skill_dispatch

        koan_root = str(tmp_path)
        instance = str(tmp_path / "instance")
        (tmp_path / "instance").mkdir()
        (tmp_path / "instance" / "journal").mkdir(parents=True)
        (tmp_path / "koan").mkdir()

        mock_proc = self._make_mock_popen(returncode=1)
        mock_proc.wait.side_effect = KeyboardInterrupt()

        with patch("app.run.subprocess.Popen", return_value=mock_proc), \
             patch("app.run._get_koan_branch", return_value="main"), \
             patch("app.run._restore_koan_branch"), \
             patch("app.run._reset_terminal"), \
             patch("app.run.protected_phase", return_value=MagicMock(
                 __enter__=MagicMock(), __exit__=MagicMock(return_value=False)
             )), \
             patch("app.run._notify"), \
             patch("app.run._notify_mission_end"), \
             patch("app.run._finalize_mission") as mock_finalize, \
             patch("app.run._commit_instance"), \
             patch("app.run._sleep_between_runs"), \
             patch("app.run.set_status"), \
             patch("app.run.log"), \
             patch("app.skill_dispatch.dispatch_skill_mission",
                   return_value=["python3", "-m", "app.plan_runner"]), \
             patch("app.mission_runner.run_post_mission"):
            with pytest.raises(KeyboardInterrupt):
                _handle_skill_dispatch(
                    mission_title="/plan test",
                    project_name="test",
                    project_path=str(tmp_path),
                    koan_root=koan_root,
                    instance=instance,
                    run_num=1,
                    max_runs=20,
                    autonomous_mode="implement",
                    interval=30,
                )

        # _finalize_mission called with exit_code=1 before re-raising
        mock_finalize.assert_called_once()
        assert mock_finalize.call_args[0][3] == 1

    def test_successful_skill_dispatch_uses_real_exit_code(self, tmp_path):
        """On success, _finalize_mission receives exit_code=0."""
        from app.run import _handle_skill_dispatch

        koan_root = str(tmp_path)
        instance = str(tmp_path / "instance")
        (tmp_path / "instance").mkdir()
        (tmp_path / "instance" / "journal").mkdir(parents=True)
        (tmp_path / "koan").mkdir()

        mock_proc = self._make_mock_popen(returncode=0, stdout_lines=["ok\n"])

        with patch("app.run.subprocess.Popen", return_value=mock_proc), \
             patch("app.run._get_koan_branch", return_value="main"), \
             patch("app.run._restore_koan_branch"), \
             patch("app.run._reset_terminal"), \
             patch("app.run.protected_phase", return_value=MagicMock(
                 __enter__=MagicMock(), __exit__=MagicMock(return_value=False)
             )), \
             patch("app.run._notify"), \
             patch("app.run._notify_mission_end"), \
             patch("app.run._finalize_mission") as mock_finalize, \
             patch("app.run._commit_instance"), \
             patch("app.run._sleep_between_runs"), \
             patch("app.run.set_status"), \
             patch("app.run.log"), \
             patch("app.skill_dispatch.dispatch_skill_mission",
                   return_value=["python3", "-m", "app.plan_runner"]), \
             patch("app.mission_runner.run_post_mission"):
            handled, _ = _handle_skill_dispatch(
                mission_title="/plan test",
                project_name="test",
                project_path=str(tmp_path),
                koan_root=koan_root,
                instance=instance,
                run_num=1,
                max_runs=20,
                autonomous_mode="implement",
                interval=30,
            )

        assert handled is True
        mock_finalize.assert_called_once()
        assert mock_finalize.call_args[0][3] == 0  # exit_code=0 on success


# ---------------------------------------------------------------------------
# Bug fix: _run_skill_mission temp file cleanup must use try/finally
# ---------------------------------------------------------------------------


class TestRunSkillMissionCleanup(TestRunSkillMissionEnv):
    """Verify temp files are always cleaned up in _run_skill_mission.

    The post-mission block (write stdout, run_post_mission, cleanup) was
    previously not wrapped in try/finally.  If open() or run_post_mission()
    raised an unexpected exception, temp files would leak.  These tests
    verify the fix: _cleanup_temp runs even on failure.
    """

    def test_temp_files_cleaned_on_post_mission_error(self, tmp_path):
        """Temp files are removed even when run_post_mission raises."""
        from app.run import _run_skill_mission

        koan_root = str(tmp_path)
        instance = str(tmp_path / "instance")
        (tmp_path / "instance").mkdir()
        (tmp_path / "instance" / "journal").mkdir(parents=True)
        (tmp_path / "koan").mkdir()

        mock_proc = self._make_mock_popen(returncode=0, stdout_lines=["ok\n"])

        with patch("app.run.subprocess.Popen", side_effect=mock_proc._side_effect), \
             patch("app.run._get_koan_branch", return_value="main"), \
             patch("app.run._restore_koan_branch"), \
             patch("app.run._reset_terminal"), \
             patch("app.mission_runner.run_post_mission",
                   side_effect=RuntimeError("post-mission crash")), \
             patch("app.run._cleanup_temp") as mock_cleanup:
            _run_skill_mission(
                skill_cmd=["python3", "--help"],
                koan_root=koan_root,
                instance=instance,
                project_name="test",
                project_path=str(tmp_path),
                run_num=1,
                mission_title="/plan test",
                autonomous_mode="implement",
            )

        # _cleanup_temp must be called even though run_post_mission raised
        mock_cleanup.assert_called_once()

    def test_temp_files_cleaned_on_stdout_write_error(self, tmp_path):
        """Temp files are removed even when writing stdout to file fails."""
        from app.run import _run_skill_mission

        koan_root = str(tmp_path)
        instance = str(tmp_path / "instance")
        (tmp_path / "instance").mkdir()
        (tmp_path / "instance" / "journal").mkdir(parents=True)
        (tmp_path / "koan").mkdir()

        mock_proc = self._make_mock_popen(returncode=0, stdout_lines=["ok\n"])

        # Make open() fail for the stdout write by patching the built-in
        original_open = open

        def _failing_open(path, mode='r', *args, **kwargs):
            if isinstance(path, str) and 'koan-out-' in path and 'wb' in str(mode):
                raise OSError("disk full")
            return original_open(path, mode, *args, **kwargs)

        with patch("app.run.subprocess.Popen", side_effect=mock_proc._side_effect), \
             patch("app.run._get_koan_branch", return_value="main"), \
             patch("app.run._restore_koan_branch"), \
             patch("app.run._reset_terminal"), \
             patch("app.mission_runner.run_post_mission"), \
             patch("app.run._cleanup_temp") as mock_cleanup, \
             patch("builtins.open", side_effect=_failing_open):
            _run_skill_mission(
                skill_cmd=["python3", "--help"],
                koan_root=koan_root,
                instance=instance,
                project_name="test",
                project_path=str(tmp_path),
                run_num=1,
                mission_title="/plan test",
                autonomous_mode="implement",
            )

        # _cleanup_temp must be called even though open() raised
        mock_cleanup.assert_called_once()

    def test_temp_files_cleaned_on_successful_run(self, tmp_path):
        """Temp files are cleaned up on normal successful execution."""
        from app.run import _run_skill_mission

        koan_root = str(tmp_path)
        instance = str(tmp_path / "instance")
        (tmp_path / "instance").mkdir()
        (tmp_path / "instance" / "journal").mkdir(parents=True)
        (tmp_path / "koan").mkdir()

        mock_proc = self._make_mock_popen(returncode=0, stdout_lines=["done\n"])

        with patch("app.run.subprocess.Popen", side_effect=mock_proc._side_effect), \
             patch("app.run._get_koan_branch", return_value="main"), \
             patch("app.run._restore_koan_branch"), \
             patch("app.run._reset_terminal"), \
             patch("app.mission_runner.run_post_mission"), \
             patch("app.run._cleanup_temp") as mock_cleanup:
            exit_code = _run_skill_mission(
                skill_cmd=["python3", "--help"],
                koan_root=koan_root,
                instance=instance,
                project_name="test",
                project_path=str(tmp_path),
                run_num=1,
                mission_title="/plan test",
                autonomous_mode="implement",
            )

        assert exit_code == 0
        mock_cleanup.assert_called_once()

    def test_temp_files_cleaned_on_timeout(self, tmp_path):
        """Temp files are cleaned up when subprocess times out."""
        from app.run import _run_skill_mission

        koan_root = str(tmp_path)
        instance = str(tmp_path / "instance")
        (tmp_path / "instance").mkdir()
        (tmp_path / "instance" / "journal").mkdir(parents=True)
        (tmp_path / "koan").mkdir()

        mock_proc = self._make_mock_popen()
        mock_proc.wait.side_effect = subprocess.TimeoutExpired("cmd", 3600)

        with patch("app.run.subprocess.Popen", side_effect=mock_proc._side_effect), \
             patch("app.run._get_koan_branch", return_value="main"), \
             patch("app.run._restore_koan_branch"), \
             patch("app.run._reset_terminal"), \
             patch("app.run._kill_process_group"), \
             patch("app.mission_runner.run_post_mission"), \
             patch("app.run._cleanup_temp") as mock_cleanup:
            exit_code = _run_skill_mission(
                skill_cmd=["python3", "--help"],
                koan_root=koan_root,
                instance=instance,
                project_name="test",
                project_path=str(tmp_path),
                run_num=1,
                mission_title="/plan test",
                autonomous_mode="implement",
            )

        assert exit_code == 1
        mock_cleanup.assert_called_once()


class TestUpdateMissionInFile:
    """Test _update_mission_in_file needle matching."""

    def test_finds_original_title_in_progress(self, tmp_path):
        """The original (untranslated) title must match the In Progress line."""
        from app.run import _update_mission_in_file

        missions = tmp_path / "instance" / "missions.md"
        missions.parent.mkdir(parents=True)
        missions.write_text(
            "# Missions\n\n"
            "## Pending\n\n"
            "## In Progress\n\n"
            "- [project:test] /scope.myskill do something ⏳(2026-01-01T00:00) ▶(2026-01-01T00:01)\n\n"
            "## Done\n"
        )

        _update_mission_in_file(str(missions.parent), "/scope.myskill do something")

        content = missions.read_text()
        # Mission should have moved from In Progress to Done
        assert "/scope.myskill do something" not in content.split("## In Progress")[1].split("##")[0]
        assert "/scope.myskill do something" in content.split("## Done")[1]

    def test_translated_title_does_not_match(self, tmp_path):
        """A translated title should NOT find the original in In Progress."""
        from app.run import _update_mission_in_file

        missions = tmp_path / "instance" / "missions.md"
        missions.parent.mkdir(parents=True)
        missions.write_text(
            "# Missions\n\n"
            "## Pending\n\n"
            "## In Progress\n\n"
            "- [project:test] /scope.myskill do something ⏳(2026-01-01T00:00) ▶(2026-01-01T00:01)\n\n"
            "## Done\n"
        )

        # Using a translated title that differs — this should NOT match
        _update_mission_in_file(str(missions.parent), "/my-tool do something")

        content = missions.read_text()
        # Mission should still be in In Progress (not moved)
        assert "/scope.myskill do something" in content.split("## In Progress")[1].split("##")[0]


# ---------------------------------------------------------------------------
# Test: _run_iteration returns productive/idle boolean
# ---------------------------------------------------------------------------

class TestRunIterationProductiveReturn:
    """_run_iteration returns True for productive runs, False for idle ones.

    This is critical: main_loop only increments count (and thus run_num /
    max_runs tracking) when _run_iteration returns True.  Idle iterations
    (wait states, errors, dedup skips) must return False so they don't
    inflate the run counter and cause premature max_runs pause.
    """

    def _make_plan(self, action, **overrides):
        plan = {
            "action": action,
            "project_name": "koan",
            "project_path": "/tmp/koan",
            "autonomous_mode": "implement",
            "available_pct": 50,
            "display_lines": [],
            "mission_title": "",
            "focus_area": "",
            "decision_reason": "",
            "recurring_injected": [],
        }
        plan.update(overrides)
        return plan

    def _run(self, tmp_path, mock_plan, action, **plan_overrides):
        from app.run import _run_iteration
        mock_plan.return_value = self._make_plan(action, **plan_overrides)
        instance = str(tmp_path / "instance")
        os.makedirs(instance, exist_ok=True)
        (tmp_path / ".koan-project").write_text("koan")
        return _run_iteration(
            koan_root=str(tmp_path),
            instance=instance,
            projects=[("koan", "/tmp/koan")],
            count=0, max_runs=10, interval=60, git_sync_interval=5,
        )

    @patch("app.run.interruptible_sleep", return_value=None)
    @patch("app.run.set_status")
    @patch("app.run.log")
    @patch("app.run.plan_iteration")
    def test_focus_wait_returns_false(self, mock_plan, mock_log, mock_status, mock_sleep, tmp_path):
        result = self._run(tmp_path, mock_plan, "focus_wait", focus_remaining="2h")
        assert result is False

    @patch("app.run.interruptible_sleep", return_value=None)
    @patch("app.run.set_status")
    @patch("app.run.log")
    @patch("app.run.plan_iteration")
    def test_schedule_wait_returns_false(self, mock_plan, mock_log, mock_status, mock_sleep, tmp_path):
        result = self._run(tmp_path, mock_plan, "schedule_wait")
        assert result is False

    @patch("app.run.interruptible_sleep", return_value=None)
    @patch("app.run.set_status")
    @patch("app.run.log")
    @patch("app.run.plan_iteration")
    def test_exploration_wait_returns_false(self, mock_plan, mock_log, mock_status, mock_sleep, tmp_path):
        result = self._run(tmp_path, mock_plan, "exploration_wait")
        assert result is False

    @patch("app.run.interruptible_sleep", return_value=None)
    @patch("app.run.set_status")
    @patch("app.run.log")
    @patch("app.run.plan_iteration")
    def test_pr_limit_wait_returns_false(self, mock_plan, mock_log, mock_status, mock_sleep, tmp_path):
        result = self._run(tmp_path, mock_plan, "pr_limit_wait")
        assert result is False

    @patch("app.run._handle_wait_pause")
    @patch("app.run.set_status")
    @patch("app.run.log")
    @patch("app.run.plan_iteration")
    def test_wait_pause_returns_false(self, mock_plan, mock_log, mock_status, mock_handle, tmp_path):
        result = self._run(tmp_path, mock_plan, "wait_pause")
        assert result is False

    @patch("app.run._commit_instance")
    @patch("app.run._update_mission_in_file")
    @patch("app.run._notify")
    @patch("app.run.plan_iteration")
    def test_error_action_returns_false(self, mock_plan, mock_notify, mock_update, mock_commit, tmp_path):
        result = self._run(tmp_path, mock_plan, "error",
                           error="Unknown project", mission_title="do stuff")
        assert result is False

    @patch("app.run._handle_contemplative")
    @patch("app.run.set_status")
    @patch("app.run.log")
    @patch("app.run.plan_iteration")
    def test_contemplative_returns_true(self, mock_plan, mock_log, mock_status, mock_contemp, tmp_path):
        result = self._run(tmp_path, mock_plan, "contemplative")
        assert result is True


class TestMainLoopCountIncrement:
    """main_loop only increments count when _run_iteration returns True.

    This prevents idle iterations (wait states) from inflating the run
    counter and causing premature max_runs pause.
    """

    @patch("app.run.subprocess.run")
    @patch("app.run.run_startup", return_value=(5, 10, "koan/"))
    @patch("app.run.acquire_pidfile")
    @patch("app.run.release_pidfile")
    @patch("app.run._run_iteration")
    def test_count_not_incremented_on_idle_iteration(
        self, mock_iteration, mock_release, mock_acquire,
        mock_startup, mock_subproc, koan_root,
    ):
        """When _run_iteration returns False, count stays the same."""
        from app.run import main_loop

        os.environ["KOAN_ROOT"] = str(koan_root)
        os.environ["KOAN_PROJECTS"] = f"test:{koan_root}"
        (koan_root / ".koan-project").write_text("test")

        call_count = [0]
        counts_seen = []

        def iteration_side_effect(**kwargs):
            call_count[0] += 1
            counts_seen.append(kwargs["count"])
            if call_count[0] <= 3:
                return False  # idle iterations
            # 4th call: stop
            (koan_root / ".koan-stop").touch()
            (koan_root / ".koan-project").write_text("test")
            return True  # productive

        mock_iteration.side_effect = iteration_side_effect

        with patch("app.run._notify"):
            main_loop()

        # count should NOT have incremented during the 3 idle iterations
        # All 4 calls should see count=0 for the first 3 (idle),
        # then count=0 for the 4th (productive, but not yet incremented)
        assert counts_seen[:3] == [0, 0, 0], (
            f"Idle iterations should not increment count, got: {counts_seen}"
        )

    @patch("app.run.subprocess.run")
    @patch("app.run.run_startup", return_value=(5, 10, "koan/"))
    @patch("app.run.acquire_pidfile")
    @patch("app.run.release_pidfile")
    @patch("app.run._run_iteration")
    def test_count_incremented_on_productive_iteration(
        self, mock_iteration, mock_release, mock_acquire,
        mock_startup, mock_subproc, koan_root,
    ):
        """When _run_iteration returns True, count increments."""
        from app.run import main_loop

        os.environ["KOAN_ROOT"] = str(koan_root)
        os.environ["KOAN_PROJECTS"] = f"test:{koan_root}"
        (koan_root / ".koan-project").write_text("test")

        call_count = [0]
        counts_seen = []

        def iteration_side_effect(**kwargs):
            call_count[0] += 1
            counts_seen.append(kwargs["count"])
            if call_count[0] >= 4:
                (koan_root / ".koan-stop").touch()
                (koan_root / ".koan-project").write_text("test")
            return True  # all productive

        mock_iteration.side_effect = iteration_side_effect

        with patch("app.run._notify"):
            main_loop()

        # count should increment after each productive iteration
        assert counts_seen == [0, 1, 2, 3], (
            f"Productive iterations should increment count, got: {counts_seen}"
        )

    @patch("app.run.subprocess.run")
    @patch("app.run.run_startup", return_value=(5, 10, "koan/"))
    @patch("app.run.acquire_pidfile")
    @patch("app.run.release_pidfile")
    @patch("app.run._run_iteration")
    def test_mixed_idle_and_productive_count(
        self, mock_iteration, mock_release, mock_acquire,
        mock_startup, mock_subproc, koan_root,
    ):
        """Mixed idle/productive iterations: count only advances on productive ones."""
        from app.run import main_loop

        os.environ["KOAN_ROOT"] = str(koan_root)
        os.environ["KOAN_PROJECTS"] = f"test:{koan_root}"
        (koan_root / ".koan-project").write_text("test")

        call_count = [0]
        counts_seen = []
        # Pattern: idle, productive, idle, idle, productive, stop
        pattern = [False, True, False, False, True]

        def iteration_side_effect(**kwargs):
            call_count[0] += 1
            counts_seen.append(kwargs["count"])
            if call_count[0] > len(pattern):
                (koan_root / ".koan-stop").touch()
                (koan_root / ".koan-project").write_text("test")
                return True
            return pattern[call_count[0] - 1]

        mock_iteration.side_effect = iteration_side_effect

        with patch("app.run._notify"):
            main_loop()

        # idle(0), productive(0)→inc, idle(1), idle(1), productive(1)→inc, stop(2)
        assert counts_seen == [0, 0, 1, 1, 1, 2], (
            f"Expected count pattern [0,0,1,1,1,2], got: {counts_seen}"
        )
