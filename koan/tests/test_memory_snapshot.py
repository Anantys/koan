"""Tests for memory snapshot export and cold-boot hydration."""

import pytest
from pathlib import Path

from app.memory_manager import MemoryManager, _parse_snapshot_sections


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _populate_instance(tmp_path):
    """Create a fully populated test instance directory."""
    instance = tmp_path / "instance"
    memory = instance / "memory"
    global_dir = memory / "global"
    projects_dir = memory / "projects"

    # Create directories
    global_dir.mkdir(parents=True)
    (projects_dir / "koan").mkdir(parents=True)
    (projects_dir / "anantys").mkdir(parents=True)
    (projects_dir / "_template").mkdir(parents=True)

    # Summary
    (memory / "summary.md").write_text(
        "# Session Summary\n\n"
        "## 2026-03-01\n\n"
        "Session 1 (projet: koan) : Implemented snapshot feature\n\n"
        "Session 2 (projet: anantys) : Fixed bug in dashboard\n\n"
        "## 2026-03-02\n\n"
        "Session 3 (projet: koan) : Added hydration logic\n",
        encoding="utf-8",
    )

    # Global files
    (global_dir / "genesis.md").write_text("# Genesis\n\nI was created.\n", encoding="utf-8")
    (global_dir / "strategy.md").write_text("# Strategy\n\nBe helpful.\n", encoding="utf-8")

    # Per-project learnings
    (projects_dir / "koan" / "learnings.md").write_text(
        "# Learnings\n\n- Use atomic writes\n- Test everything\n",
        encoding="utf-8",
    )
    (projects_dir / "anantys" / "learnings.md").write_text(
        "# Learnings\n\n- Dashboard uses Flask\n",
        encoding="utf-8",
    )

    # Soul
    (instance / "soul.md").write_text("# Soul\n\nI am Kōan.\n", encoding="utf-8")

    # Shared journal
    (instance / "shared-journal.md").write_text(
        "# Shared Journal\n\nEntry 1: First reflection.\nEntry 2: Second reflection.\n",
        encoding="utf-8",
    )

    return instance


# ---------------------------------------------------------------------------
# Phase 1: export_snapshot tests
# ---------------------------------------------------------------------------

class TestExportSnapshotFull:
    """Test export with fully populated instance."""

    def test_export_snapshot_full(self, tmp_path):
        instance = _populate_instance(tmp_path)
        mgr = MemoryManager(str(instance))

        path = mgr.export_snapshot()

        assert path.exists()
        assert path.name == "SNAPSHOT.md"
        content = path.read_text(encoding="utf-8")

        # Metadata
        assert "# Kōan Memory Snapshot" in content
        assert "Exported:" in content
        assert "Projects: anantys, koan" in content

        # Summary section
        assert "## Summary" in content
        assert "Session 1 (projet: koan)" in content
        assert "Session 3 (projet: koan)" in content

        # Global files
        assert "## Global / genesis" in content
        assert "I was created." in content
        assert "## Global / strategy" in content
        assert "Be helpful." in content

        # Per-project learnings
        assert "## Projects / anantys / learnings" in content
        assert "Dashboard uses Flask" in content
        assert "## Projects / koan / learnings" in content
        assert "Use atomic writes" in content

        # Soul
        assert "## Soul" in content
        assert "I am Kōan." in content

        # Shared journal
        assert "## Shared Journal" in content
        assert "First reflection." in content

    def test_export_snapshot_excludes_template(self, tmp_path):
        instance = _populate_instance(tmp_path)
        # Add _template learnings that should be excluded
        template_dir = instance / "memory" / "projects" / "_template"
        (template_dir / "learnings.md").write_text("# Template\n", encoding="utf-8")

        mgr = MemoryManager(str(instance))
        path = mgr.export_snapshot()
        content = path.read_text(encoding="utf-8")

        assert "_template" not in content


class TestExportSnapshotPartial:
    """Test export with only some files present."""

    def test_export_snapshot_partial(self, tmp_path):
        instance = tmp_path / "instance"
        memory = instance / "memory"
        memory.mkdir(parents=True)

        # Only summary exists
        (memory / "summary.md").write_text(
            "# Summary\n\n## 2026-03-01\n\nSession 1 : Hello\n",
            encoding="utf-8",
        )

        mgr = MemoryManager(str(instance))
        path = mgr.export_snapshot()

        content = path.read_text(encoding="utf-8")
        assert "## Summary" in content
        assert "Session 1 : Hello" in content
        # No global/projects/soul/journal sections
        assert "## Global" not in content
        assert "## Projects" not in content
        assert "## Soul" not in content
        assert "## Shared Journal" not in content


class TestExportSnapshotEmpty:
    """Test export with empty instance (no memory at all)."""

    def test_export_snapshot_empty(self, tmp_path):
        instance = tmp_path / "instance"
        instance.mkdir(parents=True)

        mgr = MemoryManager(str(instance))
        path = mgr.export_snapshot()

        content = path.read_text(encoding="utf-8")
        assert "# Kōan Memory Snapshot" in content
        assert "Projects: none" in content
        # Minimal valid snapshot
        assert "## Summary" in content


class TestExportSnapshotCaps:
    """Test that export caps content to prevent unbounded growth."""

    def test_summary_capped_at_20_sessions(self, tmp_path):
        instance = tmp_path / "instance"
        memory = instance / "memory"
        memory.mkdir(parents=True)

        # Create 30 sessions
        lines = ["# Summary\n"]
        for i in range(30):
            lines.append(f"\n## 2026-01-{i+1:02d}\n")
            lines.append(f"\nSession {i+1} : Task {i+1}\n")
        (memory / "summary.md").write_text("\n".join(lines), encoding="utf-8")

        mgr = MemoryManager(str(instance))
        path = mgr.export_snapshot()
        content = path.read_text(encoding="utf-8")

        # Should only have last 20 sessions
        assert "Session 11" in content
        assert "Session 30" in content
        assert "Session 1 :" not in content

    def test_learnings_capped_at_200_lines(self, tmp_path):
        instance = tmp_path / "instance"
        projects_dir = instance / "memory" / "projects" / "bigproject"
        projects_dir.mkdir(parents=True)

        lines = ["# Learnings\n"] + [f"- Fact {i}\n" for i in range(300)]
        (projects_dir / "learnings.md").write_text("".join(lines), encoding="utf-8")

        mgr = MemoryManager(str(instance))
        path = mgr.export_snapshot()
        content = path.read_text(encoding="utf-8")

        assert "truncated to last 200 lines" in content

    def test_shared_journal_capped_at_50_lines(self, tmp_path):
        instance = tmp_path / "instance"
        (instance / "memory").mkdir(parents=True)

        lines = [f"Line {i}" for i in range(100)]
        (instance / "shared-journal.md").write_text("\n".join(lines), encoding="utf-8")

        mgr = MemoryManager(str(instance))
        path = mgr.export_snapshot()
        content = path.read_text(encoding="utf-8")

        # Should have last 50 lines
        assert "Line 99" in content
        assert "Line 50" in content
        assert "Line 49" not in content


# ---------------------------------------------------------------------------
# Phase 2: hydrate_from_snapshot tests
# ---------------------------------------------------------------------------

class TestHydrateFull:
    """Test full hydration cycle: export → delete → hydrate → verify."""

    def test_hydrate_full(self, tmp_path):
        instance = _populate_instance(tmp_path)
        mgr = MemoryManager(str(instance))

        # Export snapshot
        mgr.export_snapshot()

        # Delete memory contents (but keep SNAPSHOT.md)
        import shutil
        for item in (instance / "memory").iterdir():
            if item.name != "SNAPSHOT.md":
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
        (instance / "soul.md").unlink()
        (instance / "shared-journal.md").unlink()

        # Hydrate
        restored = mgr.hydrate_from_snapshot()

        assert "memory/summary.md" in restored
        assert "memory/global/genesis.md" in restored
        assert "memory/global/strategy.md" in restored
        assert "memory/projects/koan/learnings.md" in restored
        assert "memory/projects/anantys/learnings.md" in restored
        assert "soul.md" in restored
        assert "shared-journal.md" in restored

        # Verify content
        summary = (instance / "memory" / "summary.md").read_text(encoding="utf-8")
        assert "Session 1 (projet: koan)" in summary

        genesis = (instance / "memory" / "global" / "genesis.md").read_text(encoding="utf-8")
        assert "I was created." in genesis

        learnings = (instance / "memory" / "projects" / "koan" / "learnings.md").read_text(encoding="utf-8")
        assert "Use atomic writes" in learnings

        soul = (instance / "soul.md").read_text(encoding="utf-8")
        assert "I am Kōan." in soul


class TestHydrateSkipsExisting:
    """Test that hydration never overwrites existing files."""

    def test_hydrate_skips_existing(self, tmp_path):
        instance = _populate_instance(tmp_path)
        mgr = MemoryManager(str(instance))

        # Export snapshot
        mgr.export_snapshot()

        # Delete only some files
        (instance / "memory" / "global" / "genesis.md").unlink()
        (instance / "shared-journal.md").unlink()

        # Modify a file that should NOT be overwritten
        (instance / "soul.md").write_text("# Modified Soul\n", encoding="utf-8")

        # Hydrate
        restored = mgr.hydrate_from_snapshot()

        # Should restore deleted files
        assert "memory/global/genesis.md" in restored
        assert "shared-journal.md" in restored

        # Should NOT overwrite existing files
        assert "soul.md" not in restored
        assert "memory/summary.md" not in restored

        # Verify existing file was preserved
        soul = (instance / "soul.md").read_text(encoding="utf-8")
        assert "Modified Soul" in soul


class TestHydrateNoSnapshot:
    """Test graceful handling when no snapshot exists."""

    def test_hydrate_no_snapshot(self, tmp_path):
        instance = tmp_path / "instance"
        instance.mkdir(parents=True)

        mgr = MemoryManager(str(instance))
        restored = mgr.hydrate_from_snapshot()

        assert restored == {}

    def test_hydrate_fallback_location(self, tmp_path):
        """Test that hydration checks instance root as fallback."""
        instance = _populate_instance(tmp_path)
        mgr = MemoryManager(str(instance))

        # Export, then move snapshot to instance root
        mgr.export_snapshot()
        snapshot_src = instance / "memory" / "SNAPSHOT.md"
        snapshot_dst = instance / "SNAPSHOT.md"
        snapshot_src.rename(snapshot_dst)

        # Delete memory contents
        import shutil
        for item in (instance / "memory").iterdir():
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
        (instance / "soul.md").unlink()
        (instance / "shared-journal.md").unlink()

        # Hydrate should find snapshot at instance root
        restored = mgr.hydrate_from_snapshot()
        assert len(restored) > 0
        assert "memory/summary.md" in restored


# ---------------------------------------------------------------------------
# Round-trip test
# ---------------------------------------------------------------------------

class TestRoundTrip:
    """Test that export → hydrate → export produces identical snapshots."""

    def test_round_trip_content(self, tmp_path):
        instance = _populate_instance(tmp_path)
        mgr = MemoryManager(str(instance))

        # First export
        mgr.export_snapshot()
        snapshot1 = (instance / "memory" / "SNAPSHOT.md").read_text(encoding="utf-8")

        # Delete and hydrate
        import shutil
        for item in (instance / "memory").iterdir():
            if item.name != "SNAPSHOT.md":
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
        (instance / "soul.md").unlink()
        (instance / "shared-journal.md").unlink()

        mgr.hydrate_from_snapshot()

        # Second export
        mgr.export_snapshot()
        snapshot2 = (instance / "memory" / "SNAPSHOT.md").read_text(encoding="utf-8")

        # Compare (strip timestamp line which will differ)
        lines1 = [l for l in snapshot1.splitlines() if not l.startswith("Exported:")]
        lines2 = [l for l in snapshot2.splitlines() if not l.startswith("Exported:")]
        assert lines1 == lines2


# ---------------------------------------------------------------------------
# Phase 3: cleanup integration test
# ---------------------------------------------------------------------------

class TestCleanupIncludesSnapshot:
    """Test that run_cleanup() produces a SNAPSHOT.md."""

    def test_cleanup_includes_snapshot(self, tmp_path):
        instance = _populate_instance(tmp_path)
        mgr = MemoryManager(str(instance))

        stats = mgr.run_cleanup()

        assert "snapshot_exported" in stats
        assert stats["snapshot_exported"] > 0
        assert (instance / "memory" / "SNAPSHOT.md").exists()


# ---------------------------------------------------------------------------
# Parser tests
# ---------------------------------------------------------------------------

class TestParseSnapshotSections:
    """Test the _parse_snapshot_sections helper."""

    def test_parse_sections(self):
        content = (
            "# Header\n\nSome intro\n\n"
            "## Summary\n\nContent A line 1\nContent A line 2\n\n"
            "## Soul\n\nContent B\n"
        )
        sections = _parse_snapshot_sections(content)

        assert "Summary" in sections
        assert "Soul" in sections
        assert "Content A line 1" in sections["Summary"]
        assert "Content B" in sections["Soul"]

    def test_parse_empty(self):
        sections = _parse_snapshot_sections("# Just a header\n")
        assert sections == {}

    def test_parse_preserves_content(self):
        content = "## Global / genesis\n\n# Genesis\n\nI was created.\n\nMore text.\n"
        sections = _parse_snapshot_sections(content)
        assert "Global / genesis" in sections
        assert "# Genesis" in sections["Global / genesis"]
        assert "More text." in sections["Global / genesis"]
