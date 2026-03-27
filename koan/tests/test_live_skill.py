"""Tests for the live progress skill handler (/live command)."""

import importlib.util
from pathlib import Path

import pytest


def _load_handler():
    """Load the live skill handler module."""
    handler_path = (
        Path(__file__).parent.parent / "skills" / "core" / "live" / "handler.py"
    )
    spec = importlib.util.spec_from_file_location("live_handler", str(handler_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_ctx(tmp_path, args=""):
    """Create a minimal SkillContext for testing."""
    from app.skills import SkillContext

    return SkillContext(koan_root=tmp_path, instance_dir=tmp_path, args=args)


class TestReadLiveProgress:
    """Tests for _read_live_progress helper."""

    def test_no_pending_file(self, tmp_path):
        mod = _load_handler()
        (tmp_path / "journal").mkdir()
        result = mod._read_live_progress(tmp_path)
        assert result is None

    def test_empty_pending_file(self, tmp_path):
        mod = _load_handler()
        pending = tmp_path / "journal" / "pending.md"
        pending.parent.mkdir(parents=True)
        pending.write_text("")
        result = mod._read_live_progress(tmp_path)
        assert result is None

    def test_whitespace_only_pending(self, tmp_path):
        mod = _load_handler()
        pending = tmp_path / "journal" / "pending.md"
        pending.parent.mkdir(parents=True)
        pending.write_text("   \n  \n  ")
        result = mod._read_live_progress(tmp_path)
        assert result is None

    def test_pending_with_header_only(self, tmp_path):
        mod = _load_handler()
        pending = tmp_path / "journal" / "pending.md"
        pending.parent.mkdir(parents=True)
        pending.write_text(
            "# Mission: do stuff\nProject: koan\nStarted: 2026-02-07\n\n---\n"
        )
        result = mod._read_live_progress(tmp_path)
        assert result is not None
        assert "Mission: do stuff" in result

    def test_pending_with_full_content(self, tmp_path):
        mod = _load_handler()
        pending = tmp_path / "journal" / "pending.md"
        pending.parent.mkdir(parents=True)
        content = (
            "# Mission: add feature\n"
            "Project: koan\n"
            "Started: 2026-02-07 08:00:00\n"
            "Run: 5/50\n"
            "Mode: deep\n"
            "\n"
            "---\n"
            "08:00 — Reading codebase\n"
            "08:05 — Creating branch\n"
            "08:10 — Writing handler\n"
        )
        pending.write_text(content)
        result = mod._read_live_progress(tmp_path)
        assert "Mission: add feature" in result
        assert "Project: koan" in result
        assert "08:00 — Reading codebase" in result
        assert "08:10 — Writing handler" in result

    def test_returns_all_progress_lines_within_limit(self, tmp_path):
        """Lines within the display limit are all shown."""
        mod = _load_handler()
        pending = tmp_path / "journal" / "pending.md"
        pending.parent.mkdir(parents=True)
        lines = "\n".join(f"09:{i:02d} — Step {i}" for i in range(15))
        pending.write_text(f"# Mission: test\n\n---\n{lines}")
        result = mod._read_live_progress(tmp_path)
        # All 15 steps should be present (within 30-line limit)
        for i in range(15):
            assert f"Step {i}" in result


    def test_truncates_long_output_showing_tail(self, tmp_path):
        """When activity exceeds the limit, only the tail is shown."""
        mod = _load_handler()
        pending = tmp_path / "journal" / "pending.md"
        pending.parent.mkdir(parents=True)
        lines = "\n".join(f"09:{i:02d} — Step {i}" for i in range(50))
        pending.write_text(f"# Mission: test\n\n---\n{lines}")
        result = mod._format_progress(mod._read_live_progress(tmp_path))
        # Should contain the tail lines
        assert "Step 49" in result
        assert "Step 48" in result
        # Should NOT contain early lines
        assert "Step 0" not in result
        # Should indicate truncation
        assert "earlier lines omitted" in result


class TestFormatProgress:
    """Tests for _format_progress — code block wrapping of activity lines."""

    def test_wraps_activity_in_code_block(self):
        mod = _load_handler()
        content = (
            "# Mission: fix bug\n"
            "Project: koan\n"
            "\n"
            "---\n"
            "10:00 — Investigating\n"
            "10:05 — Found root cause"
        )
        result = mod._format_progress(content)
        assert "```\n10:00 — Investigating\n10:05 — Found root cause\n```" in result

    def test_header_preserved_outside_code_block(self):
        mod = _load_handler()
        content = (
            "# Mission: fix bug\n"
            "Project: koan\n"
            "\n"
            "---\n"
            "10:00 — Investigating"
        )
        result = mod._format_progress(content)
        assert result.startswith("# Mission: fix bug\nProject: koan")
        assert "```" in result

    def test_no_separator_returns_content_as_is(self):
        mod = _load_handler()
        content = "# Mission: fix bug\nProject: koan"
        result = mod._format_progress(content)
        assert result == content

    def test_empty_activity_returns_content_as_is(self):
        mod = _load_handler()
        content = "# Mission: fix bug\n\n---\n"
        result = mod._format_progress(content)
        assert "```" not in result

    def test_multiple_separators_only_splits_on_first(self):
        mod = _load_handler()
        content = (
            "# Mission: fix\n"
            "\n"
            "---\n"
            "10:00 — Step 1\n"
            "---\n"
            "10:05 — Step 2"
        )
        result = mod._format_progress(content)
        # The second --- should be inside the code block
        assert "```\n10:00 — Step 1\n---\n10:05 — Step 2\n```" in result


class TestGetInProgressMissions:
    """Tests for _get_in_progress_missions — fallback when pending.md is absent."""

    def test_no_missions_file(self, tmp_path):
        mod = _load_handler()
        result = mod._get_in_progress_missions(tmp_path)
        assert result == []

    def test_empty_missions_file(self, tmp_path):
        mod = _load_handler()
        (tmp_path / "missions.md").write_text(
            "# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n"
        )
        result = mod._get_in_progress_missions(tmp_path)
        assert result == []

    def test_single_in_progress_mission(self, tmp_path):
        mod = _load_handler()
        (tmp_path / "missions.md").write_text(
            "# Missions\n\n## Pending\n\n## In Progress\n\n"
            "- [project:myapp] /audit security check ▶(2026-03-26T10:00)\n\n"
            "## Done\n"
        )
        result = mod._get_in_progress_missions(tmp_path)
        assert len(result) == 1
        project, text = result[0]
        assert project == "myapp"
        assert "/audit" in text

    def test_multiple_in_progress_missions(self, tmp_path):
        mod = _load_handler()
        (tmp_path / "missions.md").write_text(
            "# Missions\n\n## Pending\n\n## In Progress\n\n"
            "- [project:alpha] /review code ▶(2026-03-26T10:00)\n"
            "- [project:beta] fix the login bug ▶(2026-03-26T10:05)\n\n"
            "## Done\n"
        )
        result = mod._get_in_progress_missions(tmp_path)
        assert len(result) == 2
        assert result[0][0] == "alpha"
        assert result[1][0] == "beta"


class TestFormatNoOutput:
    """Tests for _format_no_output — message when mission runs but has no output."""

    def test_single_mission(self):
        mod = _load_handler()
        result = mod._format_no_output([("myapp", "/audit security")])
        assert "Mission [myapp] running: /audit security" in result
        assert "No output available yet." in result

    def test_multiple_missions(self):
        mod = _load_handler()
        result = mod._format_no_output([
            ("alpha", "/review code"),
            ("beta", "fix bug"),
        ])
        assert "Missions running:" in result
        assert "[alpha] /review code" in result
        assert "[beta] fix bug" in result
        assert "No output available yet." in result


class TestHandleLive:
    """Tests for handle() — the /live command entry point."""

    def test_no_mission_running(self, tmp_path):
        mod = _load_handler()
        (tmp_path / "journal").mkdir()
        ctx = _make_ctx(tmp_path)
        result = mod.handle(ctx)
        assert result == "No mission running."

    def test_no_journal_dir(self, tmp_path):
        mod = _load_handler()
        ctx = _make_ctx(tmp_path)
        result = mod.handle(ctx)
        assert result == "No mission running."

    def test_in_progress_mission_no_output(self, tmp_path):
        """When a mission is in progress but pending.md doesn't exist yet."""
        mod = _load_handler()
        (tmp_path / "journal").mkdir()
        (tmp_path / "missions.md").write_text(
            "# Missions\n\n## Pending\n\n## In Progress\n\n"
            "- [project:koan] /audit full security audit ▶(2026-03-26T10:00)\n\n"
            "## Done\n"
        )
        ctx = _make_ctx(tmp_path)
        result = mod.handle(ctx)
        assert "No mission running." not in result
        assert "koan" in result
        assert "/audit" in result
        assert "No output available yet." in result

    def test_in_progress_mission_empty_pending(self, tmp_path):
        """When a mission is in progress but pending.md is empty."""
        mod = _load_handler()
        pending = tmp_path / "journal" / "pending.md"
        pending.parent.mkdir(parents=True)
        pending.write_text("")
        (tmp_path / "missions.md").write_text(
            "# Missions\n\n## Pending\n\n## In Progress\n\n"
            "- [project:myapp] implement feature X ▶(2026-03-26T10:00)\n\n"
            "## Done\n"
        )
        ctx = _make_ctx(tmp_path)
        result = mod.handle(ctx)
        assert "No mission running." not in result
        assert "myapp" in result
        assert "No output available yet." in result

    def test_pending_output_takes_priority_over_missions_check(self, tmp_path):
        """When pending.md has content, it should be shown (not the fallback)."""
        mod = _load_handler()
        pending = tmp_path / "journal" / "pending.md"
        pending.parent.mkdir(parents=True)
        pending.write_text(
            "# Mission: fix bug\nProject: koan\n\n---\n"
            "10:00 — Investigating\n"
        )
        (tmp_path / "missions.md").write_text(
            "# Missions\n\n## Pending\n\n## In Progress\n\n"
            "- [project:koan] fix bug ▶(2026-03-26T10:00)\n\n"
            "## Done\n"
        )
        ctx = _make_ctx(tmp_path)
        result = mod.handle(ctx)
        assert "Investigating" in result
        assert "No output available yet." not in result

    def test_shows_progress_when_running(self, tmp_path):
        mod = _load_handler()
        pending = tmp_path / "journal" / "pending.md"
        pending.parent.mkdir(parents=True)
        pending.write_text(
            "# Mission: fix bug\nProject: koan\n\n---\n"
            "10:00 — Investigating\n"
            "10:05 — Found root cause\n"
        )
        ctx = _make_ctx(tmp_path)
        result = mod.handle(ctx)
        assert "Mission: fix bug" in result
        assert "Investigating" in result
        assert "Found root cause" in result

    def test_args_are_ignored(self, tmp_path):
        """/live takes no arguments — args are ignored gracefully."""
        mod = _load_handler()
        pending = tmp_path / "journal" / "pending.md"
        pending.parent.mkdir(parents=True)
        pending.write_text("# Mission: work\n\n---\n10:00 — doing stuff")
        ctx = _make_ctx(tmp_path, args="some random args")
        result = mod.handle(ctx)
        assert "Mission: work" in result


class TestSkillMetadata:
    """Tests for SKILL.md metadata."""

    def test_skill_md_exists(self):
        skill_path = (
            Path(__file__).parent.parent / "skills" / "core" / "live" / "SKILL.md"
        )
        assert skill_path.exists()

    def test_skill_md_has_live_command(self):
        skill_path = (
            Path(__file__).parent.parent / "skills" / "core" / "live" / "SKILL.md"
        )
        content = skill_path.read_text()
        assert "name: live" in content
        assert "handler: handler.py" in content

    def test_skill_md_has_progress_alias(self):
        skill_path = (
            Path(__file__).parent.parent / "skills" / "core" / "live" / "SKILL.md"
        )
        content = skill_path.read_text()
        assert "progress" in content

    def test_skill_discovered_by_registry(self):
        """The skill should be auto-discovered by the skills registry."""
        from app.skills import build_registry

        registry = build_registry()
        skill = registry.find_by_command("live")
        assert skill is not None
        assert skill.name == "live"

    def test_progress_alias_discovered(self):
        """The /progress alias should also resolve to the live skill."""
        from app.skills import build_registry

        registry = build_registry()
        skill = registry.find_by_command("progress")
        assert skill is not None
        assert skill.name == "live"
