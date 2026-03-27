"""Kōan snapshot skill — export memory state to SNAPSHOT.md."""


def handle(ctx):
    """Export memory snapshot and report results."""
    from app.memory_manager import MemoryManager

    mgr = MemoryManager(str(ctx.instance_dir))
    snapshot_path = mgr.export_snapshot()

    size = snapshot_path.stat().st_size
    content = snapshot_path.read_text(encoding="utf-8")
    section_count = content.count("\n## ")

    if size > 1024:
        size_display = f"{size / 1024:.1f} KB"
    else:
        size_display = f"{size} bytes"

    return f"Memory snapshot exported ({size_display}, {section_count} sections)"
