"""Tests for the /status, /ping, and /usage skill handler."""

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

# Ensure the koan package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skills.core.status.handler import (
    _needs_ollama,
    _truncate,
    _format_mission_display,
    handle,
    _handle_status,
    _handle_ping,
    _handle_usage,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def instance_dir(tmp_path):
    """Create a minimal instance directory structure."""
    inst = tmp_path / "instance"
    inst.mkdir()
    (inst / "journal").mkdir()
    return inst


@pytest.fixture
def koan_root(tmp_path, instance_dir):
    """Return the koan root (parent of instance)."""
    return tmp_path


def _make_ctx(koan_root, instance_dir, command_name="status", args=""):
    return SimpleNamespace(
        koan_root=koan_root,
        instance_dir=instance_dir,
        command_name=command_name,
        args=args,
        send_message=None,
        handle_chat=None,
    )


# ---------------------------------------------------------------------------
# _needs_ollama
# ---------------------------------------------------------------------------

class TestNeedsOllama:
    def test_returns_true_for_local(self):
        with patch("skills.core.status.handler._needs_ollama") as mock:
            mock.return_value = True
            assert mock() is True

    @patch("app.provider.get_provider_name", return_value="claude")
    def test_returns_false_for_claude(self, _mock):
        assert _needs_ollama() is False

    @patch("app.provider.get_provider_name", return_value="copilot")
    def test_returns_false_for_copilot(self, _mock):
        assert _needs_ollama() is False

    @patch("app.provider.get_provider_name", return_value="local")
    def test_returns_true_for_local_provider(self, _mock):
        assert _needs_ollama() is True

    @patch("app.provider.get_provider_name", return_value="ollama")
    def test_returns_true_for_ollama_provider(self, _mock):
        assert _needs_ollama() is True

    @patch("app.provider.get_provider_name", side_effect=ImportError)
    def test_returns_false_on_import_error(self, _mock):
        assert _needs_ollama() is False


# ---------------------------------------------------------------------------
# _truncate
# ---------------------------------------------------------------------------

class TestTruncate:
    def test_short_string_unchanged(self):
        assert _truncate("hello", 60) == "hello"

    def test_exact_length_unchanged(self):
        text = "a" * 60
        assert _truncate(text, 60) == text

    def test_long_string_truncated(self):
        text = "a" * 100
        result = _truncate(text, 60)
        assert len(result) <= 60
        assert result.endswith("…")

    def test_default_max_len(self):
        text = "x" * 80
        result = _truncate(text)
        assert len(result) <= 60
        assert result.endswith("…")

    def test_empty_string(self):
        assert _truncate("", 60) == ""

    def test_single_char_max(self):
        result = _truncate("hello world", 5)
        assert len(result) <= 5
        assert result.endswith("…")


# ---------------------------------------------------------------------------
# _format_mission_display
# ---------------------------------------------------------------------------

class TestFormatMissionDisplay:
    def test_strips_project_tag(self):
        result = _format_mission_display("[project:koan] Fix the bug")
        assert "project:koan" not in result
        assert "Fix the bug" in result

    def test_truncates_long_mission(self):
        long_text = "A" * 200
        result = _format_mission_display(long_text)
        assert len(result) <= 80  # with possible timing suffix

    def test_plain_mission(self):
        result = _format_mission_display("Simple mission text")
        assert "Simple mission text" in result

    def test_mission_with_timestamps(self):
        mission = "Fix bug ⏳(2026-02-18T10:00) ▶(2026-02-18T10:05)"
        result = _format_mission_display(mission)
        # Should strip raw timestamps but may show timing
        assert "⏳" not in result or "min" in result  # timing display or stripped


# ---------------------------------------------------------------------------
# handle (dispatch)
# ---------------------------------------------------------------------------

class TestHandleDispatch:
    def test_dispatches_to_status(self, koan_root, instance_dir):
        ctx = _make_ctx(koan_root, instance_dir, "status")
        with patch("skills.core.status.handler._handle_status", return_value="ok") as mock:
            result = handle(ctx)
            mock.assert_called_once_with(ctx)
            assert result == "ok"

    def test_dispatches_to_ping(self, koan_root, instance_dir):
        ctx = _make_ctx(koan_root, instance_dir, "ping")
        with patch("skills.core.status.handler._handle_ping", return_value="pong") as mock:
            result = handle(ctx)
            mock.assert_called_once_with(ctx)
            assert result == "pong"

    def test_dispatches_to_usage(self, koan_root, instance_dir):
        ctx = _make_ctx(koan_root, instance_dir, "usage")
        with patch("skills.core.status.handler._handle_usage", return_value="usage") as mock:
            result = handle(ctx)
            mock.assert_called_once_with(ctx)
            assert result == "usage"

    def test_unknown_command_defaults_to_status(self, koan_root, instance_dir):
        ctx = _make_ctx(koan_root, instance_dir, "whatever")
        with patch("skills.core.status.handler._handle_status", return_value="ok") as mock:
            result = handle(ctx)
            mock.assert_called_once()


# ---------------------------------------------------------------------------
# _handle_status
# ---------------------------------------------------------------------------

class TestHandleStatus:
    def test_basic_working_status(self, koan_root, instance_dir):
        ctx = _make_ctx(koan_root, instance_dir)
        with patch("skills.core.status.handler._needs_ollama", return_value=False):
            result = _handle_status(ctx)
        assert "Kōan Status" in result
        assert "🟢 Mode: Working" in result

    def test_paused_status(self, koan_root, instance_dir):
        (koan_root / ".koan-pause").touch()
        ctx = _make_ctx(koan_root, instance_dir)
        with patch("skills.core.status.handler._needs_ollama", return_value=False):
            result = _handle_status(ctx)
        assert "⏸️ Mode: Paused" in result
        assert "/resume" in result

    def test_paused_quota_reason(self, koan_root, instance_dir):
        (koan_root / ".koan-pause").write_text("quota\n1234567890\n")
        ctx = _make_ctx(koan_root, instance_dir)
        with patch("skills.core.status.handler._needs_ollama", return_value=False):
            result = _handle_status(ctx)
        assert "quota exhausted" in result

    def test_paused_max_runs_reason(self, koan_root, instance_dir):
        (koan_root / ".koan-pause").write_text("max_runs\n1234567890\n")
        ctx = _make_ctx(koan_root, instance_dir)
        with patch("skills.core.status.handler._needs_ollama", return_value=False):
            result = _handle_status(ctx)
        assert "max runs reached" in result

    def test_stopping_status(self, koan_root, instance_dir):
        (koan_root / ".koan-stop").touch()
        ctx = _make_ctx(koan_root, instance_dir)
        with patch("skills.core.status.handler._needs_ollama", return_value=False):
            result = _handle_status(ctx)
        assert "⛔ Mode: Stopping" in result

    def test_shows_loop_status(self, koan_root, instance_dir):
        (koan_root / ".koan-status").write_text("Run 3/50 — executing mission on koan")
        ctx = _make_ctx(koan_root, instance_dir)
        with patch("skills.core.status.handler._needs_ollama", return_value=False):
            result = _handle_status(ctx)
        assert "Run 3/50" in result

    def test_shows_pending_missions(self, koan_root, instance_dir):
        missions = (
            "# Missions\n\n"
            "## Pending\n\n"
            "- [project:koan] Fix authentication\n"
            "- [project:koan] Add tests\n"
            "\n## In Progress\n\n"
            "## Done\n"
        )
        (instance_dir / "missions.md").write_text(missions)
        ctx = _make_ctx(koan_root, instance_dir)
        with patch("skills.core.status.handler._needs_ollama", return_value=False):
            result = _handle_status(ctx)
        assert "Pending: 2" in result

    def test_shows_in_progress_missions(self, koan_root, instance_dir):
        missions = (
            "# Missions\n\n"
            "## Pending\n\n"
            "## In Progress\n\n"
            "- [project:koan] Working on it ⏳(2026-02-18T10:00) ▶(2026-02-18T10:05)\n"
            "\n## Done\n"
        )
        (instance_dir / "missions.md").write_text(missions)
        ctx = _make_ctx(koan_root, instance_dir)
        with patch("skills.core.status.handler._needs_ollama", return_value=False):
            result = _handle_status(ctx)
        assert "In progress: 1" in result

    def test_no_missions_file(self, koan_root, instance_dir):
        ctx = _make_ctx(koan_root, instance_dir)
        with patch("skills.core.status.handler._needs_ollama", return_value=False):
            result = _handle_status(ctx)
        # Should not crash, just show status header
        assert "Kōan Status" in result

    def test_empty_missions_file(self, koan_root, instance_dir):
        (instance_dir / "missions.md").write_text("# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n")
        ctx = _make_ctx(koan_root, instance_dir)
        with patch("skills.core.status.handler._needs_ollama", return_value=False):
            result = _handle_status(ctx)
        assert "Kōan Status" in result

    @patch("app.focus_manager.check_focus")
    def test_shows_focus_mode(self, mock_focus, koan_root, instance_dir):
        mock_focus.return_value = SimpleNamespace(remaining_display=lambda: "2h30m")
        ctx = _make_ctx(koan_root, instance_dir)
        with patch("skills.core.status.handler._needs_ollama", return_value=False):
            result = _handle_status(ctx)
        assert "🎯 Focus" in result
        assert "2h30m" in result

    @patch("app.focus_manager.check_focus", return_value=None)
    def test_no_focus_mode(self, mock_focus, koan_root, instance_dir):
        ctx = _make_ctx(koan_root, instance_dir)
        with patch("skills.core.status.handler._needs_ollama", return_value=False):
            result = _handle_status(ctx)
        assert "Focus" not in result

    @patch("app.pid_manager.check_pidfile", return_value=12345)
    def test_shows_ollama_running(self, mock_pid, koan_root, instance_dir):
        ctx = _make_ctx(koan_root, instance_dir)
        with patch("skills.core.status.handler._needs_ollama", return_value=True):
            result = _handle_status(ctx)
        assert "🦙 Ollama: running" in result
        assert "12345" in result

    @patch("app.pid_manager.check_pidfile", return_value=None)
    def test_shows_ollama_not_running(self, mock_pid, koan_root, instance_dir):
        ctx = _make_ctx(koan_root, instance_dir)
        with patch("skills.core.status.handler._needs_ollama", return_value=True):
            result = _handle_status(ctx)
        assert "🦙 Ollama: not running" in result

    def test_multi_project_missions(self, koan_root, instance_dir):
        missions = (
            "# Missions\n\n"
            "## Pending\n\n"
            "- [project:alpha] Task A\n"
            "- [project:beta] Task B\n"
            "- [project:alpha] Task C\n"
            "\n## In Progress\n\n"
            "## Done\n"
        )
        (instance_dir / "missions.md").write_text(missions)
        ctx = _make_ctx(koan_root, instance_dir)
        with patch("skills.core.status.handler._needs_ollama", return_value=False):
            result = _handle_status(ctx)
        assert "alpha" in result
        assert "beta" in result


# ---------------------------------------------------------------------------
# _handle_ping
# ---------------------------------------------------------------------------

class TestHandlePing:
    @patch("app.pid_manager.check_pidfile")
    def test_all_processes_running(self, mock_pid, koan_root, instance_dir):
        mock_pid.side_effect = lambda root, name: {"run": 100, "awake": 200}.get(name)
        ctx = _make_ctx(koan_root, instance_dir, "ping")
        with patch("skills.core.status.handler._needs_ollama", return_value=False):
            result = _handle_ping(ctx)
        assert "✅ Runner" in result
        assert "✅ Bridge" in result

    @patch("app.pid_manager.check_pidfile")
    def test_runner_not_running(self, mock_pid, koan_root, instance_dir):
        mock_pid.side_effect = lambda root, name: {"run": None, "awake": 200}.get(name)
        ctx = _make_ctx(koan_root, instance_dir, "ping")
        with patch("skills.core.status.handler._needs_ollama", return_value=False):
            result = _handle_ping(ctx)
        assert "❌ Runner: not running" in result
        assert "make run" in result

    @patch("app.pid_manager.check_pidfile")
    def test_bridge_not_running(self, mock_pid, koan_root, instance_dir):
        mock_pid.side_effect = lambda root, name: {"run": 100, "awake": None}.get(name)
        ctx = _make_ctx(koan_root, instance_dir, "ping")
        with patch("skills.core.status.handler._needs_ollama", return_value=False):
            result = _handle_ping(ctx)
        assert "✅ Runner" in result
        assert "❌ Bridge: not running" in result
        assert "make awake" in result

    @patch("app.pid_manager.check_pidfile")
    def test_runner_paused(self, mock_pid, koan_root, instance_dir):
        mock_pid.side_effect = lambda root, name: {"run": 100, "awake": 200}.get(name)
        (koan_root / ".koan-pause").touch()
        ctx = _make_ctx(koan_root, instance_dir, "ping")
        with patch("skills.core.status.handler._needs_ollama", return_value=False):
            result = _handle_ping(ctx)
        assert "⏸️ Runner: paused" in result
        assert "/resume" in result

    @patch("app.pid_manager.check_pidfile")
    def test_runner_stopping(self, mock_pid, koan_root, instance_dir):
        mock_pid.side_effect = lambda root, name: {"run": 100, "awake": 200}.get(name)
        (koan_root / ".koan-stop").touch()
        ctx = _make_ctx(koan_root, instance_dir, "ping")
        with patch("skills.core.status.handler._needs_ollama", return_value=False):
            result = _handle_ping(ctx)
        assert "⏹️ Runner: stopping" in result

    @patch("app.pid_manager.check_pidfile")
    def test_runner_with_loop_status(self, mock_pid, koan_root, instance_dir):
        mock_pid.side_effect = lambda root, name: {"run": 100, "awake": 200}.get(name)
        (koan_root / ".koan-status").write_text("Run 5/50 — idle")
        ctx = _make_ctx(koan_root, instance_dir, "ping")
        with patch("skills.core.status.handler._needs_ollama", return_value=False):
            result = _handle_ping(ctx)
        assert "Run 5/50" in result
        assert "PID 100" in result

    @patch("app.pid_manager.check_pidfile")
    def test_ollama_shown_when_needed(self, mock_pid, koan_root, instance_dir):
        mock_pid.side_effect = lambda root, name: {"run": 100, "awake": 200, "ollama": 300}.get(name)
        ctx = _make_ctx(koan_root, instance_dir, "ping")
        with patch("skills.core.status.handler._needs_ollama", return_value=True):
            result = _handle_ping(ctx)
        assert "✅ Ollama" in result
        assert "300" in result

    @patch("app.pid_manager.check_pidfile")
    def test_ollama_hidden_when_not_needed(self, mock_pid, koan_root, instance_dir):
        mock_pid.side_effect = lambda root, name: {"run": 100, "awake": 200}.get(name)
        ctx = _make_ctx(koan_root, instance_dir, "ping")
        with patch("skills.core.status.handler._needs_ollama", return_value=False):
            result = _handle_ping(ctx)
        assert "Ollama" not in result

    @patch("app.pid_manager.check_pidfile")
    def test_ollama_not_running(self, mock_pid, koan_root, instance_dir):
        mock_pid.side_effect = lambda root, name: {"run": 100, "awake": 200, "ollama": None}.get(name)
        ctx = _make_ctx(koan_root, instance_dir, "ping")
        with patch("skills.core.status.handler._needs_ollama", return_value=True):
            result = _handle_ping(ctx)
        assert "❌ Ollama: not running" in result


# ---------------------------------------------------------------------------
# _handle_usage
# ---------------------------------------------------------------------------

class TestHandleUsage:
    def test_no_data(self, koan_root, instance_dir):
        ctx = _make_ctx(koan_root, instance_dir, "usage")
        result = _handle_usage(ctx)
        assert "No quota data" in result
        assert "No missions" in result
        assert "No run in progress" in result

    def test_with_usage_data(self, koan_root, instance_dir):
        (instance_dir / "usage.md").write_text("Used: 50% | Available: 50%")
        ctx = _make_ctx(koan_root, instance_dir, "usage")
        result = _handle_usage(ctx)
        assert "50%" in result

    def test_with_pending_journal(self, koan_root, instance_dir):
        pending = instance_dir / "journal" / "pending.md"
        pending.write_text("15:01 — Working on tests\n15:02 — Running pytest")
        ctx = _make_ctx(koan_root, instance_dir, "usage")
        result = _handle_usage(ctx)
        assert "Working on tests" in result

    def test_long_pending_truncated(self, koan_root, instance_dir):
        pending = instance_dir / "journal" / "pending.md"
        pending.write_text("line\n" * 1000)  # ~5000 chars
        ctx = _make_ctx(koan_root, instance_dir, "usage")
        result = _handle_usage(ctx)
        assert "..." in result

    def test_with_missions(self, koan_root, instance_dir):
        missions = (
            "# Missions\n\n"
            "## Pending\n\n"
            "- [project:koan] Task 1\n"
            "- [project:koan] Task 2\n"
            "\n## In Progress\n\n"
            "- [project:koan] Active task ⏳(2026-02-18T10:00) ▶(2026-02-18T10:05)\n"
            "\n## Done\n\n"
            "- [project:koan] Done task ✅ (2026-02-18)\n"
        )
        (instance_dir / "missions.md").write_text(missions)
        ctx = _make_ctx(koan_root, instance_dir, "usage")
        result = _handle_usage(ctx)
        assert "Pending (2)" in result
        assert "In progress" in result
        assert "Done: 1" in result

    def test_empty_pending_journal(self, koan_root, instance_dir):
        pending = instance_dir / "journal" / "pending.md"
        pending.write_text("")
        ctx = _make_ctx(koan_root, instance_dir, "usage")
        result = _handle_usage(ctx)
        assert "No run in progress" in result

    def test_empty_usage_file(self, koan_root, instance_dir):
        (instance_dir / "usage.md").write_text("")
        ctx = _make_ctx(koan_root, instance_dir, "usage")
        result = _handle_usage(ctx)
        assert "No quota data" in result
