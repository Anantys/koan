#!/usr/bin/env python3
"""Rename a project across projects.yaml and all instance/ files.

Usage:
    python -m app.rename_project <old_name> <new_name> [--apply]

Without --apply, runs in dry-run mode (shows what would change).
"""

import os
import re
import sys
import shutil
from pathlib import Path

import yaml


def get_koan_root() -> Path:
    root = os.environ.get("KOAN_ROOT")
    if not root:
        print("Error: KOAN_ROOT not set", file=sys.stderr)
        sys.exit(1)
    return Path(root)


def rename_project_key_in_yaml(yaml_path: Path, old_name: str, new_name: str) -> str:
    """Rename a project key in projects.yaml while preserving formatting.

    Uses regex replacement to avoid YAML round-trip reformatting.
    """
    text = yaml_path.read_text()
    # Match the project key at the correct indentation level (2 spaces under projects:)
    pattern = re.compile(rf"^(  ){re.escape(old_name)}(:)", re.MULTILINE)
    new_text = pattern.sub(rf"\g<1>{new_name}\2", text)
    if new_text == text:
        raise ValueError(f"Project '{old_name}' not found in {yaml_path}")
    return new_text


def find_instance_files(instance_dir: Path) -> list:
    """Find all text files in instance/ that may contain project references."""
    files = []
    for path in sorted(instance_dir.rglob("*")):
        if not path.is_file():
            continue
        # Skip binary files and hidden temp files
        if path.name.startswith(".koan-"):
            continue
        suffix = path.suffix.lower()
        if suffix in (".md", ".json", ".jsonl", ".yaml", ".yml", ".txt"):
            files.append(path)
    return files


def replace_in_file(path: Path, old_name: str, new_name: str) -> list:
    """Replace project references in a file. Returns list of (line_num, old_line, new_line)."""
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, PermissionError):
        return []

    changes = []
    lines = text.split("\n")
    for i, line in enumerate(lines):
        new_line = line
        # [project:old_name] tags
        new_line = new_line.replace(f"[project:{old_name}]", f"[project:{new_name}]")
        new_line = new_line.replace(f"[projet:{old_name}]", f"[projet:{new_name}]")
        # "project": "old_name" in JSON
        new_line = new_line.replace(f'"project": "{old_name}"', f'"project": "{new_name}"')
        new_line = new_line.replace(f'"project":"{old_name}"', f'"project":"{new_name}"')
        # project: old_name in YAML context
        new_line = re.sub(
            rf'\bproject:\s*{re.escape(old_name)}\b',
            f'project: {new_name}',
            new_line,
        )
        if new_line != line:
            changes.append((i + 1, line.strip(), new_line.strip()))
            lines[i] = new_line

    return changes


def rename_memory_dir(instance_dir: Path, old_name: str, new_name: str, dry_run: bool) -> bool:
    """Rename memory/projects/<old>/ to memory/projects/<new>/."""
    old_dir = instance_dir / "memory" / "projects" / old_name
    new_dir = instance_dir / "memory" / "projects" / new_name
    if old_dir.is_dir():
        if dry_run:
            print(f"  RENAME DIR  {old_dir.relative_to(instance_dir)} -> {new_dir.relative_to(instance_dir)}")
        else:
            if new_dir.exists():
                print(f"  WARNING: {new_dir} already exists, merging...", file=sys.stderr)
                for f in old_dir.iterdir():
                    shutil.move(str(f), str(new_dir / f.name))
                old_dir.rmdir()
            else:
                old_dir.rename(new_dir)
            print(f"  RENAMED DIR {old_dir.relative_to(instance_dir)} -> {new_dir.relative_to(instance_dir)}")
        return True
    return False


def rename_journal_files(instance_dir: Path, old_name: str, new_name: str, dry_run: bool) -> list:
    """Rename journal files named after the project (e.g., journal/2026-03-25/old_name.md)."""
    journal_dir = instance_dir / "journal"
    renamed = []
    if not journal_dir.exists():
        return renamed

    for path in sorted(journal_dir.rglob(f"{old_name}.md")):
        new_path = path.with_name(f"{new_name}.md")
        if dry_run:
            print(f"  RENAME FILE {path.relative_to(instance_dir)} -> {new_path.name}")
        else:
            path.rename(new_path)
            print(f"  RENAMED FILE {path.relative_to(instance_dir)} -> {new_path.name}")
        renamed.append((path, new_path))
    return renamed


def run_rename(koan_root: Path, old_name: str, new_name: str, dry_run: bool = True):
    """Execute the full rename operation."""
    yaml_path = koan_root / "projects.yaml"
    instance_dir = koan_root / "instance"

    if not yaml_path.exists():
        print(f"Error: {yaml_path} not found", file=sys.stderr)
        sys.exit(1)
    if not instance_dir.exists():
        print(f"Error: {instance_dir} not found", file=sys.stderr)
        sys.exit(1)

    # Validate old project exists
    with open(yaml_path) as f:
        config = yaml.safe_load(f)
    projects = config.get("projects", {})
    if old_name not in projects:
        print(f"Error: project '{old_name}' not found in projects.yaml", file=sys.stderr)
        print(f"Available projects: {', '.join(projects.keys())}", file=sys.stderr)
        sys.exit(1)
    if new_name in projects:
        print(f"Error: project '{new_name}' already exists in projects.yaml", file=sys.stderr)
        sys.exit(1)

    mode = "DRY RUN" if dry_run else "APPLYING"
    print(f"\n=== {mode}: Rename '{old_name}' -> '{new_name}' ===\n")

    # 1. projects.yaml
    print("[projects.yaml]")
    new_yaml = rename_project_key_in_yaml(yaml_path, old_name, new_name)
    if dry_run:
        print(f"  REPLACE key '{old_name}:' -> '{new_name}:'")
    else:
        yaml_path.write_text(new_yaml)
        print(f"  REPLACED key '{old_name}:' -> '{new_name}:'")

    # 2. Memory directory
    print("\n[memory/projects/]")
    if not rename_memory_dir(instance_dir, old_name, new_name, dry_run):
        print(f"  (no directory for '{old_name}')")

    # 3. Journal files
    print("\n[journal files]")
    renamed = rename_journal_files(instance_dir, old_name, new_name, dry_run)
    if not renamed:
        print(f"  (no journal files named '{old_name}.md')")

    # 4. Content replacements in all instance files
    print("\n[file contents]")
    total_changes = 0
    files = find_instance_files(instance_dir)
    for path in files:
        changes = replace_in_file(path, old_name, new_name)
        if changes:
            rel = path.relative_to(instance_dir)
            print(f"  {rel} ({len(changes)} replacement{'s' if len(changes) > 1 else ''})")
            for line_num, old_line, new_line in changes[:3]:
                print(f"    L{line_num}: {old_line[:80]}")
            if len(changes) > 3:
                print(f"    ... and {len(changes) - 3} more")
            if not dry_run:
                text = path.read_text(encoding="utf-8")
                text = text.replace(f"[project:{old_name}]", f"[project:{new_name}]")
                text = text.replace(f"[projet:{old_name}]", f"[projet:{new_name}]")
                text = text.replace(f'"project": "{old_name}"', f'"project": "{new_name}"')
                text = text.replace(f'"project":"{old_name}"', f'"project":"{new_name}"')
                text = re.sub(
                    rf'\bproject:\s*{re.escape(old_name)}\b',
                    f'project: {new_name}',
                    text,
                )
                path.write_text(text, encoding="utf-8")
            total_changes += len(changes)

    print(f"\n--- Total: {total_changes} content replacement{'s' if total_changes != 1 else ''} across instance/ ---")

    if dry_run:
        print(f"\nThis was a dry run. To apply, re-run with --apply")


def main():
    if len(sys.argv) < 3:
        print("Usage: python -m app.rename_project <old_name> <new_name> [--apply]")
        print("\nRenames a project in projects.yaml and all instance/ files.")
        print("Default: dry-run mode. Add --apply to execute changes.")
        sys.exit(1)

    old_name = sys.argv[1]
    new_name = sys.argv[2]
    apply = "--apply" in sys.argv

    koan_root = get_koan_root()
    run_rename(koan_root, old_name, new_name, dry_run=not apply)


if __name__ == "__main__":
    main()
