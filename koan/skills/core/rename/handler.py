"""Kōan rename skill — rename a project across all files.

Usage: /rename <old_name> <new_name>

Renames the project in projects.yaml, memory directory, journal files,
and all project references in instance/ files.
"""

import re

import yaml


def handle(ctx):
    """Handle /rename command."""
    args = ctx.args.strip()
    if not args:
        return (
            "Usage: /rename <old_name> <new_name>\n\n"
            "Renames a project everywhere: projects.yaml, memory, journals, "
            "and all instance files.\n\n"
            "Examples:\n"
            "  /rename anantys-back aback\n"
            "  /rename my-long-project mlp"
        )

    parts = args.split()
    if len(parts) != 2:
        return "Usage: /rename <old_name> <new_name>"

    old_name, new_name = parts

    koan_root = ctx.koan_root
    yaml_path = koan_root / "projects.yaml"
    instance_dir = ctx.instance_dir

    # Validate
    if not yaml_path.exists():
        return "projects.yaml not found."

    with open(yaml_path) as f:
        config = yaml.safe_load(f)

    projects = config.get("projects", {})
    if old_name not in projects:
        available = ", ".join(sorted(projects.keys()))
        return f"Project '{old_name}' not found.\nAvailable: {available}"
    if new_name in projects:
        return f"Project '{new_name}' already exists in projects.yaml."

    if ctx.send_message:
        ctx.send_message(f"Renaming '{old_name}' → '{new_name}'...")

    from app.rename_project import (
        rename_project_key_in_yaml,
        rename_memory_dir,
        rename_journal_files,
        find_instance_files,
        replace_in_file,
    )

    results = []

    # 1. projects.yaml
    new_yaml = rename_project_key_in_yaml(yaml_path, old_name, new_name)
    yaml_path.write_text(new_yaml)
    results.append("projects.yaml: key renamed")

    # 2. Memory directory
    if rename_memory_dir(instance_dir, old_name, new_name, dry_run=False):
        results.append("memory directory: renamed")

    # 3. Journal files
    renamed_journals = rename_journal_files(instance_dir, old_name, new_name, dry_run=False)
    if renamed_journals:
        results.append(f"journal files: {len(renamed_journals)} renamed")

    # 4. Content replacements
    files = find_instance_files(instance_dir)
    total_changes = 0
    for path in files:
        changes = replace_in_file(path, old_name, new_name)
        if changes:
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

    if total_changes:
        results.append(f"file contents: {total_changes} replacement{'s' if total_changes != 1 else ''}")

    summary = "\n".join(f"  ✓ {r}" for r in results)
    return f"Project renamed: '{old_name}' → '{new_name}'\n\n{summary}"
