"""Tests for the logs skill handler (/logs command)."""

import importlib.util
from pathlib import Path

import pytest


def _load_handler():
    """Load the logs skill handler module."""
    handler_path = (
        Path(__file__).parent.parent / "skills" / "core" / "logs" / "handler.py"
    )
    spec = importlib.util.spec_from_file_location("logs_handler", str(handler_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_ctx(tmp_path, args=""):
    """Create a minimal SkillContext for testing."""
    from app.skills import SkillContext

    return SkillContext(koan_root=tmp_path, instance_dir=tmp_path / "instance", args=args)


class TestTail:
    """Tests for _tail helper."""

    def test_file_not_found(self, tmp_path):
        mod = _load_handler()
        result = mod._tail(tmp_path / "nonexistent.log")
        assert result is None

    def test_empty_file(self, tmp_path):
        mod = _load_handler()
        f = tmp_path / "empty.log"
        f.write_text("")
        result = mod._tail(f)
        assert result is None

    def test_fewer_lines_than_limit(self, tmp_path):
        mod = _load_handler()
        f = tmp_path / "short.log"
        f.write_text("line1\nline2\nline3\n")
        result = mod._tail(f)
        assert result == ["line1", "line2", "line3"]

    def test_exactly_10_lines(self, tmp_path):
        mod = _load_handler()
        f = tmp_path / "exact.log"
        lines = [f"line{i}" for i in range(10)]
        f.write_text("\n".join(lines) + "\n")
        result = mod._tail(f)
        assert len(result) == 10

    def test_more_than_10_lines(self, tmp_path):
        mod = _load_handler()
        f = tmp_path / "long.log"
        lines = [f"line{i}" for i in range(20)]
        f.write_text("\n".join(lines) + "\n")
        result = mod._tail(f)
        assert len(result) == 10
        assert result[0] == "line10"
        assert result[-1] == "line19"

    def test_strips_ansi_codes(self, tmp_path):
        mod = _load_handler()
        f = tmp_path / "colored.log"
        f.write_text(
            "\x1b[34m[init]\x1b[0m Token: ...K1YkUu4I\n"
            "\x1b[32m[ok]\x1b[0m Ready\n"
            "\x1b[1;31mERROR\x1b[0m something broke\n"
        )
        result = mod._tail(f)
        assert result == [
            "[init] Token: ...K1YkUu4I",
            "[ok] Ready",
            "ERROR something broke",
        ]


class TestHandle:
    """Tests for handle() — the /logs command entry point."""

    def test_no_logs_dir(self, tmp_path):
        mod = _load_handler()
        ctx = _make_ctx(tmp_path)
        result = mod.handle(ctx)
        assert "No log files found" in result

    def test_empty_logs_dir(self, tmp_path):
        mod = _load_handler()
        (tmp_path / "logs").mkdir()
        ctx = _make_ctx(tmp_path)
        result = mod.handle(ctx)
        assert "No log files found" in result

    def test_run_log_only(self, tmp_path):
        mod = _load_handler()
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        (logs_dir / "run.log").write_text("Starting agent loop\nPicking mission\n")
        ctx = _make_ctx(tmp_path)
        result = mod.handle(ctx)
        assert "📋 run" in result
        assert "```" in result
        assert "Starting agent loop" in result
        assert "📋 awake" not in result

    def test_both_logs(self, tmp_path):
        mod = _load_handler()
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        (logs_dir / "run.log").write_text("run line 1\nrun line 2\n")
        (logs_dir / "awake.log").write_text("awake line 1\nawake line 2\n")
        ctx = _make_ctx(tmp_path)
        result = mod.handle(ctx)
        assert "📋 run" in result
        assert "📋 awake" in result
        assert "run line 1" in result
        assert "awake line 1" in result

    def test_code_block_wrapping(self, tmp_path):
        mod = _load_handler()
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        (logs_dir / "run.log").write_text("hello\n")
        ctx = _make_ctx(tmp_path)
        result = mod.handle(ctx)
        assert "```\nhello\n```" in result

    def test_truncates_long_logs(self, tmp_path):
        mod = _load_handler()
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        lines = "\n".join(f"log entry {i}" for i in range(50))
        (logs_dir / "run.log").write_text(lines + "\n")
        ctx = _make_ctx(tmp_path)
        result = mod.handle(ctx)
        # Should only show last 10 lines
        assert "log entry 40" in result
        assert "log entry 49" in result
        assert "log entry 39" not in result


class TestSkillMetadata:
    """Tests for SKILL.md metadata."""

    def test_skill_md_exists(self):
        skill_path = (
            Path(__file__).parent.parent / "skills" / "core" / "logs" / "SKILL.md"
        )
        assert skill_path.exists()

    def test_skill_md_has_logs_command(self):
        skill_path = (
            Path(__file__).parent.parent / "skills" / "core" / "logs" / "SKILL.md"
        )
        content = skill_path.read_text()
        assert "name: logs" in content
        assert "handler: handler.py" in content
        assert "group: status" in content

    def test_skill_discovered_by_registry(self):
        """The skill should be auto-discovered by the skills registry."""
        from app.skills import build_registry

        registry = build_registry()
        skill = registry.find_by_command("logs")
        assert skill is not None
        assert skill.name == "logs"
