#!/usr/bin/env python3
"""
Kōan — Migration runner

Discovers and runs numbered migration scripts from koan/migrations/.
Tracks completed migrations via touch files in instance/.migrations/.

Migration scripts follow the naming convention: NNNN_description.py
Each script must define a `migrate(instance_dir: Path) -> None` function.

Usage:
    python3 -m app.migration_runner          # run from koan/ directory
    python3 -m app.migration_runner --list   # show migration status
"""

import importlib.util
import os
import re
import sys
from pathlib import Path

KOAN_ROOT = Path(os.environ.get("KOAN_ROOT", ""))
INSTANCE_DIR = KOAN_ROOT / "instance" if KOAN_ROOT else Path()

# Migrations live in koan/migrations/ (version-controlled)
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

# Tracking directory lives inside instance/ (gitignored, per-user)
TRACKING_DIR = INSTANCE_DIR / ".migrations"

# Pattern: 0001_some_description.py
MIGRATION_PATTERN = re.compile(r"^(\d{4})_[\w]+\.py$")


def discover_migrations() -> list[tuple[str, Path]]:
    """Return sorted list of (migration_id, path) for all migration scripts."""
    if not MIGRATIONS_DIR.is_dir():
        return []
    migrations = []
    for entry in sorted(MIGRATIONS_DIR.iterdir()):
        match = MIGRATION_PATTERN.match(entry.name)
        if match and entry.is_file():
            migration_id = match.group(1)
            migrations.append((migration_id, entry))
    return migrations


def is_applied(migration_id: str, tracking_dir: Path | None = None) -> bool:
    """Check if a migration has already been applied."""
    track = tracking_dir or TRACKING_DIR
    return (track / migration_id).exists()


def mark_applied(migration_id: str, tracking_dir: Path | None = None) -> None:
    """Mark a migration as applied by creating a touch file."""
    track = tracking_dir or TRACKING_DIR
    track.mkdir(parents=True, exist_ok=True)
    (track / migration_id).touch()


def run_migration(migration_path: Path, instance_dir: Path) -> None:
    """Load and execute a single migration script."""
    spec = importlib.util.spec_from_file_location(
        f"migration_{migration_path.stem}", migration_path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    if not hasattr(module, "migrate"):
        raise AttributeError(
            f"Migration {migration_path.name} missing required migrate() function"
        )

    module.migrate(instance_dir)


def run_pending_migrations(
    instance_dir: Path | None = None,
    migrations_dir: Path | None = None,
    tracking_dir: Path | None = None,
) -> list[str]:
    """Run all pending migrations in order.

    Returns list of migration IDs that were applied.
    """
    inst = instance_dir or INSTANCE_DIR
    mig_dir = migrations_dir or MIGRATIONS_DIR
    track = tracking_dir or TRACKING_DIR

    if not inst.is_dir():
        return []

    # Discover migrations from the specified directory
    if not mig_dir.is_dir():
        return []

    migrations = []
    for entry in sorted(mig_dir.iterdir()):
        match = MIGRATION_PATTERN.match(entry.name)
        if match and entry.is_file():
            migrations.append((match.group(1), entry))

    applied = []
    for migration_id, migration_path in migrations:
        if is_applied(migration_id, track):
            continue
        try:
            run_migration(migration_path, inst)
            mark_applied(migration_id, track)
            applied.append(migration_id)
        except Exception as e:
            print(f"[migration] ERROR: {migration_path.name} failed: {e}", file=sys.stderr)
            break  # Stop on first failure

    return applied


def list_migrations(
    migrations_dir: Path | None = None,
    tracking_dir: Path | None = None,
) -> list[tuple[str, str, bool]]:
    """List all migrations with their status.

    Returns list of (migration_id, filename, is_applied).
    """
    mig_dir = migrations_dir or MIGRATIONS_DIR
    track = tracking_dir or TRACKING_DIR

    if not mig_dir.is_dir():
        return []

    result = []
    for entry in sorted(mig_dir.iterdir()):
        match = MIGRATION_PATTERN.match(entry.name)
        if match and entry.is_file():
            mid = match.group(1)
            result.append((mid, entry.name, is_applied(mid, track)))
    return result


if __name__ == "__main__":
    if not os.environ.get("KOAN_ROOT") or not INSTANCE_DIR.is_dir():
        print("KOAN_ROOT not set or instance/ not found.", file=sys.stderr)
        sys.exit(1)

    if "--list" in sys.argv:
        migrations = list_migrations()
        if not migrations:
            print("No migrations found.")
        for mid, name, done in migrations:
            status = "applied" if done else "pending"
            print(f"  [{status}] {name}")
    else:
        applied = run_pending_migrations()
        if applied:
            print(f"[migration] Applied {len(applied)} migration(s): {', '.join(applied)}")
