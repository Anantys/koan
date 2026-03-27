"""Tests for the /status, /ping, /usage core skill handlers."""

from unittest.mock import patch, MagicMock

import pytest

from app.skills import SkillContext
from app.response_cache import _format_cache


@pytest.fixture(autouse=True)
def _clear_cache_for_status():
    """Reset format cache between tests."""
    _format_cache.clear()
    yield
    _format_cache.clear()


def _make_ctx(command_name, instance_dir, koan_root=None):
    """Create a minimal SkillContext for testing."""
    ctx = MagicMock(spec=SkillContext)
    ctx.command_name = command_name
    ctx.instance_dir = instance_dir
    ctx.koan_root = koan_root or instance_dir.parent
    ctx.args = ""
    return ctx


# ---------------------------------------------------------------------------
# handle() dispatch
# ---------------------------------------------------------------------------

class TestStatusDispatch:
    """Test the top-level handle() dispatcher."""

    def test_dispatch_to_status(self, tmp_path):
        instance = tmp_path / "instance"
        instance.mkdir()
        from skills.core.status.handler import handle
        ctx = _make_ctx("status", instance, tmp_path)
        result = handle(ctx)
        assert "Kōan Status" in result

    def test_dispatch_to_ping(self, tmp_path):
        instance = tmp_path / "instance"
        instance.mkdir()
        from skills.core.status.handler import handle
        ctx = _make_ctx("ping", instance, tmp_path)
        with patch("app.pid_manager.check_pidfile", return_value=None):
            result = handle(ctx)
        assert "Runner" in result

    def test_dispatch_to_usage(self, tmp_path):
        instance = tmp_path / "instance"
        instance.mkdir()
        from skills.core.status.handler import handle
        ctx = _make_ctx("usage", instance, tmp_path)
        result = handle(ctx)
        assert "Quota" in result


# ---------------------------------------------------------------------------
# _handle_status()
# ---------------------------------------------------------------------------

class TestHandleStatus:
    """Test /status output under various states."""

    def test_working_mode_no_files(self, tmp_path):
        """No pause/stop files = Working mode."""
        instance = tmp_path / "instance"
        instance.mkdir()
        from skills.core.status.handler import _handle_status
        ctx = _make_ctx("status", instance, tmp_path)
        result = _handle_status(ctx)
        assert "Active" in result
        assert "Paused" not in result

    def test_paused_mode_generic(self, tmp_path):
        """Pause file without reason = generic pause."""
        instance = tmp_path / "instance"
        instance.mkdir()
        (tmp_path / ".koan-pause").touch()
        from skills.core.status.handler import _handle_status
        ctx = _make_ctx("status", instance, tmp_path)
        result = _handle_status(ctx)
        assert "Paused" in result
        assert "/resume" in result

    def test_paused_mode_quota(self, tmp_path):
        """Pause with quota reason shows specific message."""
        instance = tmp_path / "instance"
        instance.mkdir()
        (tmp_path / ".koan-pause").write_text("quota\n1234567890\nResets at 12:00")
        from skills.core.status.handler import _handle_status
        ctx = _make_ctx("status", instance, tmp_path)
        result = _handle_status(ctx)
        assert "quota" in result.lower()

    def test_paused_mode_max_runs(self, tmp_path):
        """Pause with max_runs reason shows specific message."""
        instance = tmp_path / "instance"
        instance.mkdir()
        (tmp_path / ".koan-pause").write_text("max_runs\n1234567890\n")
        from skills.core.status.handler import _handle_status
        ctx = _make_ctx("status", instance, tmp_path)
        result = _handle_status(ctx)
        assert "max runs" in result.lower()

    def test_stopping_mode(self, tmp_path):
        """Stop file = Stopping mode."""
        instance = tmp_path / "instance"
        instance.mkdir()
        (tmp_path / ".koan-stop").touch()
        from skills.core.status.handler import _handle_status
        ctx = _make_ctx("status", instance, tmp_path)
        result = _handle_status(ctx)
        assert "Stopping" in result

    def test_stop_takes_precedence_over_pause(self, tmp_path):
        """If both stop and pause exist, stop wins."""
        instance = tmp_path / "instance"
        instance.mkdir()
        (tmp_path / ".koan-stop").touch()
        (tmp_path / ".koan-pause").touch()
        from skills.core.status.handler import _handle_status
        ctx = _make_ctx("status", instance, tmp_path)
        result = _handle_status(ctx)
        assert "Stopping" in result
        assert "Paused" not in result

    def test_loop_status_shown(self, tmp_path):
        """When .koan-status exists, its content is included."""
        instance = tmp_path / "instance"
        instance.mkdir()
        (tmp_path / ".koan-status").write_text("idle — no pending missions")
        from skills.core.status.handler import _handle_status
        ctx = _make_ctx("status", instance, tmp_path)
        result = _handle_status(ctx)
        assert "idle" in result

    def test_missions_shown(self, tmp_path):
        """Pending and in-progress missions are displayed."""
        instance = tmp_path / "instance"
        instance.mkdir()
        missions = instance / "missions.md"
        missions.write_text(
            "# Missions\n\n"
            "## Pending\n\n"
            "- [project:koan] fix the bug\n"
            "- [project:koan] add tests\n\n"
            "## In Progress\n\n"
            "- [project:koan] refactor module\n\n"
            "## Done\n\n"
            "- old task\n"
        )
        from skills.core.status.handler import _handle_status
        ctx = _make_ctx("status", instance, tmp_path)
        result = _handle_status(ctx)
        assert "Pending: 2" in result
        assert "In progress: 1" in result
        # Project tags should be stripped from display
        assert "[project:koan]" not in result
        assert "fix the bug" in result

    def test_no_missions_file(self, tmp_path):
        """If missions.md doesn't exist, still shows status."""
        instance = tmp_path / "instance"
        instance.mkdir()
        from skills.core.status.handler import _handle_status
        ctx = _make_ctx("status", instance, tmp_path)
        result = _handle_status(ctx)
        assert "Kōan Status" in result

    def test_empty_missions(self, tmp_path):
        """missions.md with only Done items shows no pending/in-progress."""
        instance = tmp_path / "instance"
        instance.mkdir()
        missions = instance / "missions.md"
        missions.write_text(
            "# Missions\n\n"
            "## Pending\n\n"
            "## In Progress\n\n"
            "## Done\n\n"
            "- old task\n"
        )
        from skills.core.status.handler import _handle_status
        ctx = _make_ctx("status", instance, tmp_path)
        result = _handle_status(ctx)
        assert "Pending" not in result
        assert "In progress" not in result

    def test_multi_project_missions(self, tmp_path):
        """Missions from different projects are grouped separately."""
        instance = tmp_path / "instance"
        instance.mkdir()
        missions = instance / "missions.md"
        missions.write_text(
            "# Missions\n\n"
            "## Pending\n\n"
            "- [project:koan] fix the bug\n"
            "- [project:webapp] update CSS\n\n"
            "## In Progress\n\n"
            "## Done\n"
        )
        from skills.core.status.handler import _handle_status
        ctx = _make_ctx("status", instance, tmp_path)
        result = _handle_status(ctx)
        assert "koan" in result
        assert "webapp" in result

    def test_long_mission_truncated(self, tmp_path):
        """Long mission descriptions are truncated with ellipsis."""
        instance = tmp_path / "instance"
        instance.mkdir()
        long_desc = "a" * 100
        missions = instance / "missions.md"
        missions.write_text(
            "# Missions\n\n"
            "## Pending\n\n"
            f"- [project:koan] {long_desc}\n\n"
            "## In Progress\n\n"
            f"- [project:koan] {long_desc}\n\n"
            "## Done\n"
        )
        from skills.core.status.handler import _handle_status
        ctx = _make_ctx("status", instance, tmp_path)
        result = _handle_status(ctx)
        # Full 100-char text should NOT appear
        assert long_desc not in result
        # Ellipsis should appear for truncated lines
        assert "…" in result

    def test_short_mission_not_truncated(self, tmp_path):
        """Short mission descriptions are shown in full."""
        instance = tmp_path / "instance"
        instance.mkdir()
        missions = instance / "missions.md"
        missions.write_text(
            "# Missions\n\n"
            "## Pending\n\n"
            "## In Progress\n\n"
            "- [project:koan] fix the small bug\n\n"
            "## Done\n"
        )
        from skills.core.status.handler import _handle_status
        ctx = _make_ctx("status", instance, tmp_path)
        result = _handle_status(ctx)
        assert "fix the small bug" in result
        assert "…" not in result


# ---------------------------------------------------------------------------
# _truncate()
# ---------------------------------------------------------------------------

class TestTruncate:
    """Test the _truncate() helper."""

    def test_short_text_unchanged(self):
        from skills.core.status.handler import _truncate
        assert _truncate("hello world") == "hello world"

    def test_exact_limit_unchanged(self):
        from skills.core.status.handler import _truncate
        text = "x" * 60
        assert _truncate(text) == text

    def test_over_limit_truncated(self):
        from skills.core.status.handler import _truncate
        text = "a" * 80
        result = _truncate(text)
        assert result.endswith("…")
        assert len(result) == 60  # 59 chars + 1 ellipsis char

    def test_custom_max_len(self):
        from skills.core.status.handler import _truncate
        result = _truncate("hello world", max_len=5)
        assert result == "hell…"

    def test_trailing_space_stripped_before_ellipsis(self):
        from skills.core.status.handler import _truncate
        # If truncation lands on a space, it should be stripped
        result = _truncate("hello world this is long", max_len=6)
        assert result == "hello…"

    def test_empty_string(self):
        from skills.core.status.handler import _truncate
        assert _truncate("") == ""


# ---------------------------------------------------------------------------
# _handle_ping()
# ---------------------------------------------------------------------------

class TestHandlePing:
    """Test /ping process liveness check via PID files."""

    def test_both_alive(self, tmp_path):
        """Both runner and bridge alive = green status with PIDs."""
        from skills.core.status.handler import _handle_ping
        ctx = _make_ctx("ping", tmp_path, tmp_path)
        with patch("app.pid_manager.check_pidfile") as mock_check:
            mock_check.side_effect = lambda root, name: 1234 if name == "run" else 5678
            result = _handle_ping(ctx)
        assert "Runner: alive (PID 1234)" in result
        assert "Bridge: alive (PID 5678)" in result
        assert "not running" not in result

    def test_runner_dead_bridge_alive(self, tmp_path):
        """Runner dead, bridge alive."""
        from skills.core.status.handler import _handle_ping
        ctx = _make_ctx("ping", tmp_path, tmp_path)
        with patch("app.pid_manager.check_pidfile") as mock_check:
            mock_check.side_effect = lambda root, name: 5678 if name == "awake" else None
            result = _handle_ping(ctx)
        assert "Runner: not running" in result
        assert "make run" in result
        assert "Bridge: alive (PID 5678)" in result

    def test_runner_alive_bridge_dead(self, tmp_path):
        """Runner alive, bridge dead."""
        from skills.core.status.handler import _handle_ping
        ctx = _make_ctx("ping", tmp_path, tmp_path)
        with patch("app.pid_manager.check_pidfile") as mock_check:
            mock_check.side_effect = lambda root, name: 1234 if name == "run" else None
            result = _handle_ping(ctx)
        assert "Runner: alive (PID 1234)" in result
        assert "Bridge: not running" in result
        assert "make awake" in result

    def test_both_dead(self, tmp_path):
        """Both processes dead = full down status."""
        from skills.core.status.handler import _handle_ping
        ctx = _make_ctx("ping", tmp_path, tmp_path)
        with patch("app.pid_manager.check_pidfile", return_value=None):
            result = _handle_ping(ctx)
        assert "Runner: not running" in result
        assert "Bridge: not running" in result
        assert "make run" in result
        assert "make awake" in result

    def test_runner_paused(self, tmp_path):
        """Runner alive + pause file = paused status with PID."""
        (tmp_path / ".koan-pause").touch()
        from skills.core.status.handler import _handle_ping
        ctx = _make_ctx("ping", tmp_path, tmp_path)
        with patch("app.pid_manager.check_pidfile") as mock_check:
            mock_check.side_effect = lambda root, name: 1234 if name == "run" else 5678
            result = _handle_ping(ctx)
        assert "paused" in result.lower()
        assert "PID 1234" in result
        assert "/resume" in result

    def test_runner_stopping(self, tmp_path):
        """Runner alive + stop file = stopping status with PID."""
        (tmp_path / ".koan-stop").touch()
        from skills.core.status.handler import _handle_ping
        ctx = _make_ctx("ping", tmp_path, tmp_path)
        with patch("app.pid_manager.check_pidfile") as mock_check:
            mock_check.side_effect = lambda root, name: 1234 if name == "run" else 5678
            result = _handle_ping(ctx)
        assert "stopping" in result.lower()
        assert "PID 1234" in result

    def test_runner_with_loop_status(self, tmp_path):
        """Runner alive + .koan-status = shows loop state."""
        (tmp_path / ".koan-status").write_text("executing mission 3/25")
        from skills.core.status.handler import _handle_ping
        ctx = _make_ctx("ping", tmp_path, tmp_path)
        with patch("app.pid_manager.check_pidfile") as mock_check:
            mock_check.side_effect = lambda root, name: 1234 if name == "run" else 5678
            result = _handle_ping(ctx)
        assert "executing mission 3/25" in result
        assert "PID 1234" in result

    def test_runner_alive_no_status_file(self, tmp_path):
        """Runner alive without .koan-status = generic 'alive' message."""
        from skills.core.status.handler import _handle_ping
        ctx = _make_ctx("ping", tmp_path, tmp_path)
        with patch("app.pid_manager.check_pidfile") as mock_check:
            mock_check.side_effect = lambda root, name: 1234 if name == "run" else None
            result = _handle_ping(ctx)
        assert "Runner: alive (PID 1234)" in result

    def test_runner_alive_empty_status_file(self, tmp_path):
        """Runner alive with empty .koan-status = generic 'alive' message."""
        (tmp_path / ".koan-status").write_text("")
        from skills.core.status.handler import _handle_ping
        ctx = _make_ctx("ping", tmp_path, tmp_path)
        with patch("app.pid_manager.check_pidfile") as mock_check:
            mock_check.side_effect = lambda root, name: 1234 if name == "run" else None
            result = _handle_ping(ctx)
        assert "Runner: alive (PID 1234)" in result

    def test_dispatch_routes_to_ping(self, tmp_path):
        """handle() dispatches 'ping' command to _handle_ping."""
        instance = tmp_path / "instance"
        instance.mkdir()
        from skills.core.status.handler import handle
        ctx = _make_ctx("ping", instance, tmp_path)
        with patch("app.pid_manager.check_pidfile", return_value=None):
            result = handle(ctx)
        assert "Runner" in result


# ---------------------------------------------------------------------------
# _handle_usage()
# ---------------------------------------------------------------------------

class TestHandleUsage:
    """Test /usage detailed quota and progress display."""

    def test_no_usage_file(self, tmp_path):
        """No usage.md = fallback message."""
        instance = tmp_path / "instance"
        instance.mkdir()
        from skills.core.status.handler import _handle_usage
        ctx = _make_ctx("usage", instance, tmp_path)
        result = _handle_usage(ctx)
        assert "No quota data" in result

    def test_usage_file_present(self, tmp_path):
        """usage.md content is displayed."""
        instance = tmp_path / "instance"
        instance.mkdir()
        (instance / "usage.md").write_text("Session: 45%\nWeekly: 12%\n")
        from skills.core.status.handler import _handle_usage
        ctx = _make_ctx("usage", instance, tmp_path)
        result = _handle_usage(ctx)
        assert "45%" in result
        assert "Quota" in result

    def test_empty_usage_file(self, tmp_path):
        """Empty usage.md = fallback message."""
        instance = tmp_path / "instance"
        instance.mkdir()
        (instance / "usage.md").write_text("")
        from skills.core.status.handler import _handle_usage
        ctx = _make_ctx("usage", instance, tmp_path)
        result = _handle_usage(ctx)
        assert "No quota data" in result

    def test_missions_in_usage(self, tmp_path):
        """Missions are included in /usage output."""
        instance = tmp_path / "instance"
        instance.mkdir()
        missions = instance / "missions.md"
        missions.write_text(
            "# Missions\n\n"
            "## Pending\n\n"
            "- task one\n"
            "- task two\n\n"
            "## In Progress\n\n"
            "- current task\n\n"
            "## Done\n\n"
            "- done1\n- done2\n- done3\n"
        )
        from skills.core.status.handler import _handle_usage
        ctx = _make_ctx("usage", instance, tmp_path)
        result = _handle_usage(ctx)
        assert "Pending (2)" in result
        assert "In progress" in result
        assert "Done: 3" in result

    def test_no_missions_file_in_usage(self, tmp_path):
        """No missions.md = 'No missions' message."""
        instance = tmp_path / "instance"
        instance.mkdir()
        from skills.core.status.handler import _handle_usage
        ctx = _make_ctx("usage", instance, tmp_path)
        result = _handle_usage(ctx)
        assert "No missions" in result

    def test_pending_md_included(self, tmp_path):
        """Current run progress from pending.md is included."""
        instance = tmp_path / "instance"
        instance.mkdir()
        journal_dir = instance / "journal"
        journal_dir.mkdir()
        pending = journal_dir / "pending.md"
        pending.write_text("# Mission: fix something\n---\n10:00 — Reading code\n10:05 — Writing tests")
        from skills.core.status.handler import _handle_usage
        ctx = _make_ctx("usage", instance, tmp_path)
        result = _handle_usage(ctx)
        assert "fix something" in result
        assert "Current" in result

    def test_no_pending_md(self, tmp_path):
        """No pending.md = 'No run in progress' message."""
        instance = tmp_path / "instance"
        instance.mkdir()
        from skills.core.status.handler import _handle_usage
        ctx = _make_ctx("usage", instance, tmp_path)
        result = _handle_usage(ctx)
        assert "No run in progress" in result

    def test_long_pending_truncated(self, tmp_path):
        """Very long pending.md is truncated."""
        instance = tmp_path / "instance"
        instance.mkdir()
        journal_dir = instance / "journal"
        journal_dir.mkdir()
        pending = journal_dir / "pending.md"
        pending.write_text("x" * 2000)
        from skills.core.status.handler import _handle_usage
        ctx = _make_ctx("usage", instance, tmp_path)
        result = _handle_usage(ctx)
        # Content should be truncated to last 1500 chars
        assert "..." in result


# ---------------------------------------------------------------------------
# _needs_ollama() helper
# ---------------------------------------------------------------------------

class TestNeedsOllama:
    """Test the _needs_ollama() provider detection helper."""

    def test_local_provider_needs_ollama(self):
        from skills.core.status.handler import _needs_ollama
        with patch("app.provider.get_provider_name", return_value="local"):
            assert _needs_ollama() is True

    def test_ollama_provider_needs_ollama(self):
        from skills.core.status.handler import _needs_ollama
        with patch("app.provider.get_provider_name", return_value="ollama"):
            assert _needs_ollama() is True

    def test_claude_provider_does_not_need_ollama(self):
        from skills.core.status.handler import _needs_ollama
        with patch("app.provider.get_provider_name", return_value="claude"):
            assert _needs_ollama() is False

    def test_copilot_provider_does_not_need_ollama(self):
        from skills.core.status.handler import _needs_ollama
        with patch("app.provider.get_provider_name", return_value="copilot"):
            assert _needs_ollama() is False

    def test_import_error_returns_false(self):
        """If provider module can't be imported, assume no ollama needed."""
        from skills.core.status.handler import _needs_ollama
        with patch("app.provider.get_provider_name", side_effect=ImportError):
            assert _needs_ollama() is False


# ---------------------------------------------------------------------------
# /ping with ollama
# ---------------------------------------------------------------------------

class TestHandlePingOllama:
    """Test /ping output when using local/ollama providers."""

    def test_ping_shows_ollama_when_local_provider(self, tmp_path):
        """With local provider, ping shows ollama status line."""
        from skills.core.status.handler import _handle_ping
        ctx = _make_ctx("ping", tmp_path, tmp_path)

        def mock_check(root, name):
            return {"run": 1234, "awake": 5678, "ollama": 9999}.get(name)

        with patch("app.pid_manager.check_pidfile", side_effect=mock_check), \
             patch("app.provider.get_provider_name", return_value="local"):
            result = _handle_ping(ctx)

        assert "Ollama: alive (PID 9999)" in result
        assert "Runner" in result
        assert "Bridge" in result

    def test_ping_shows_ollama_dead_when_not_running(self, tmp_path):
        """With local provider, ollama not running shows error."""
        from skills.core.status.handler import _handle_ping
        ctx = _make_ctx("ping", tmp_path, tmp_path)

        def mock_check(root, name):
            return {"run": 1234, "awake": 5678}.get(name)

        with patch("app.pid_manager.check_pidfile", side_effect=mock_check), \
             patch("app.provider.get_provider_name", return_value="local"):
            result = _handle_ping(ctx)

        assert "Ollama: not running" in result
        assert "ollama serve" in result

    def test_ping_hides_ollama_with_claude_provider(self, tmp_path):
        """With claude provider, no ollama line shown."""
        from skills.core.status.handler import _handle_ping
        ctx = _make_ctx("ping", tmp_path, tmp_path)

        with patch("app.pid_manager.check_pidfile", return_value=None), \
             patch("app.provider.get_provider_name", return_value="claude"):
            result = _handle_ping(ctx)

        assert "Ollama" not in result

    def test_ping_hides_ollama_with_copilot_provider(self, tmp_path):
        """With copilot provider, no ollama line shown."""
        from skills.core.status.handler import _handle_ping
        ctx = _make_ctx("ping", tmp_path, tmp_path)

        with patch("app.pid_manager.check_pidfile", return_value=None), \
             patch("app.provider.get_provider_name", return_value="copilot"):
            result = _handle_ping(ctx)

        assert "Ollama" not in result

    def test_ping_all_three_alive_local_provider(self, tmp_path):
        """Full stack alive with local provider shows 3 green lines."""
        from skills.core.status.handler import _handle_ping
        ctx = _make_ctx("ping", tmp_path, tmp_path)

        def mock_check(root, name):
            return {"run": 100, "awake": 200, "ollama": 300}.get(name)

        with patch("app.pid_manager.check_pidfile", side_effect=mock_check), \
             patch("app.provider.get_provider_name", return_value="local"):
            result = _handle_ping(ctx)

        assert result.count("✅") == 3
        assert "Runner" in result
        assert "Bridge" in result
        assert "Ollama" in result

    def test_ping_all_three_dead_local_provider(self, tmp_path):
        """Full stack dead with local provider shows 3 red lines."""
        from skills.core.status.handler import _handle_ping
        ctx = _make_ctx("ping", tmp_path, tmp_path)

        with patch("app.pid_manager.check_pidfile", return_value=None), \
             patch("app.provider.get_provider_name", return_value="local"):
            result = _handle_ping(ctx)

        assert result.count("❌") == 3
        assert "Ollama: not running" in result


# ---------------------------------------------------------------------------
# /status with ollama
# ---------------------------------------------------------------------------

class TestHandleStatusOllama:
    """Test /status output when using local/ollama providers."""

    def test_status_shows_ollama_running(self, tmp_path):
        """With local provider, /status shows ollama process info."""
        instance = tmp_path / "instance"
        instance.mkdir()
        from skills.core.status.handler import _handle_status
        ctx = _make_ctx("status", instance, tmp_path)

        with patch("app.provider.get_provider_name", return_value="local"), \
             patch("app.pid_manager.check_pidfile", return_value=4242):
            result = _handle_status(ctx)

        assert "Ollama: running (PID 4242)" in result

    def test_status_shows_ollama_not_running(self, tmp_path):
        """With local provider, /status shows ollama down."""
        instance = tmp_path / "instance"
        instance.mkdir()
        from skills.core.status.handler import _handle_status
        ctx = _make_ctx("status", instance, tmp_path)

        with patch("app.provider.get_provider_name", return_value="local"), \
             patch("app.pid_manager.check_pidfile", return_value=None):
            result = _handle_status(ctx)

        assert "Ollama: not running" in result

    def test_status_hides_ollama_with_claude(self, tmp_path):
        """With claude provider, /status has no ollama info."""
        instance = tmp_path / "instance"
        instance.mkdir()
        from skills.core.status.handler import _handle_status
        ctx = _make_ctx("status", instance, tmp_path)

        with patch("app.provider.get_provider_name", return_value="claude"):
            result = _handle_status(ctx)

        assert "Ollama" not in result
        assert "ollama" not in result.lower()

    def test_status_ollama_with_ollama_provider(self, tmp_path):
        """Provider 'ollama' also triggers ollama status line."""
        instance = tmp_path / "instance"
        instance.mkdir()
        from skills.core.status.handler import _handle_status
        ctx = _make_ctx("status", instance, tmp_path)

        with patch("app.provider.get_provider_name", return_value="ollama"), \
             patch("app.pid_manager.check_pidfile", return_value=7777):
            result = _handle_status(ctx)

        assert "Ollama: running (PID 7777)" in result


# ---------------------------------------------------------------------------
# Cache stats in /status
# ---------------------------------------------------------------------------

class TestHandleStatusCache:
    """Test cache stats display in /status output."""

    def test_cache_stats_shown_when_used(self, tmp_path):
        """When cache has hits/misses, /status shows the stats."""
        instance = tmp_path / "instance"
        instance.mkdir()
        from skills.core.status.handler import _handle_status

        # Simulate some cache activity
        _format_cache.put("k1", "v1")
        _format_cache.get("k1")  # hit
        _format_cache.get("k1")  # hit
        _format_cache.get("miss")  # miss

        ctx = _make_ctx("status", instance, tmp_path)
        result = _handle_status(ctx)
        assert "Cache: 2 hits / 1 misses" in result

    def test_cache_stats_hidden_when_unused(self, tmp_path):
        """When cache is empty (no activity), no cache line shown."""
        instance = tmp_path / "instance"
        instance.mkdir()
        from skills.core.status.handler import _handle_status

        ctx = _make_ctx("status", instance, tmp_path)
        result = _handle_status(ctx)
        assert "Cache" not in result


# ---------------------------------------------------------------------------
# Usage staleness check
# ---------------------------------------------------------------------------

class TestCheckUsageStaleness:
    """Test _check_usage_staleness() health check."""

    def test_no_usage_file(self, tmp_path):
        """Missing usage.md warns about 75% default."""
        from skills.core.status.handler import _check_usage_staleness
        result = _check_usage_staleness(tmp_path)
        assert "no data" in result
        assert "75%" in result

    def test_fresh_usage_file(self, tmp_path):
        """Recently updated usage.md shows minutes."""
        usage = tmp_path / "usage.md"
        usage.write_text("Session (5hr) : 25% (reset in 3h)")
        from skills.core.status.handler import _check_usage_staleness
        result = _check_usage_staleness(tmp_path)
        assert "m old" in result
        assert "⚠️" not in result

    def test_stale_usage_file(self, tmp_path):
        """usage.md older than 6h triggers warning with fallback note."""
        import os
        usage = tmp_path / "usage.md"
        usage.write_text("Session (5hr) : 25% (reset in 3h)")
        # Set mtime to 8 hours ago
        old_time = __import__("time").time() - 8 * 3600
        os.utime(usage, (old_time, old_time))
        from skills.core.status.handler import _check_usage_staleness
        result = _check_usage_staleness(tmp_path)
        assert "⚠️" in result
        assert "stale" in result
        assert "75% fallback" in result

    def test_moderately_old_usage_file(self, tmp_path):
        """usage.md between 1h and 6h shows hours without warning."""
        import os
        usage = tmp_path / "usage.md"
        usage.write_text("Session (5hr) : 25% (reset in 3h)")
        old_time = __import__("time").time() - 3 * 3600
        os.utime(usage, (old_time, old_time))
        from skills.core.status.handler import _check_usage_staleness
        result = _check_usage_staleness(tmp_path)
        assert "h old" in result
        assert "⚠️" not in result


# ---------------------------------------------------------------------------
# GitHub notification queue depth check
# ---------------------------------------------------------------------------

class TestCheckGithubNotifications:
    """Test _check_github_notifications() health check."""

    def test_zero_notifications(self):
        """Empty notification list shows 0 unread."""
        from skills.core.status.handler import _check_github_notifications
        with patch("app.github.api", return_value="[]"):
            result = _check_github_notifications()
        assert result == "📬 GitHub: 0 unread"

    def test_some_notifications(self):
        """Small number of notifications shows count."""
        import json
        from skills.core.status.handler import _check_github_notifications
        fake = json.dumps([{"id": str(i)} for i in range(5)])
        with patch("app.github.api", return_value=fake):
            result = _check_github_notifications()
        assert result == "📬 GitHub: 5 unread"

    def test_many_notifications_no_warning(self):
        """20+ unread notifications shows mailbox icon, not warning."""
        import json
        from skills.core.status.handler import _check_github_notifications
        fake = json.dumps([{"id": str(i)} for i in range(25)])
        with patch("app.github.api", return_value=fake):
            result = _check_github_notifications()
        assert "📬" in result
        assert "⚠️" not in result
        assert "25 unread" in result

    def test_overflow_notifications(self):
        """100+ unread shows overflow indicator with mailbox icon."""
        import json
        from skills.core.status.handler import _check_github_notifications
        fake = json.dumps([{"id": str(i)} for i in range(100)])
        with patch("app.github.api", return_value=fake):
            result = _check_github_notifications()
        assert "📬" in result
        assert "100+" in result

    def test_api_failure_returns_none(self):
        """API errors produce None (item is silently omitted)."""
        from skills.core.status.handler import _check_github_notifications
        with patch("app.github.api", side_effect=RuntimeError("auth failed")):
            result = _check_github_notifications()
        assert result is None

    def test_empty_response(self):
        """Empty string response = 0 unread."""
        from skills.core.status.handler import _check_github_notifications
        with patch("app.github.api", return_value=""):
            result = _check_github_notifications()
        assert "0 unread" in result
