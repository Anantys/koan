"""
Kōan -- Core file integrity checker.

Verifies that critical unversioned files (projects.yaml, instance/, .env)
are not accidentally deleted during mission execution. These files are
gitignored and cannot be recovered from version control.

Used as pre/post guards around Claude CLI invocations.
"""

import sys
from pathlib import Path
from typing import List, Optional, Set, Tuple


# Paths relative to KOAN_ROOT that must never be deleted.
# Directories end with "/" to distinguish from files.
CORE_PATHS = (
    "instance/",
    "instance/missions.md",
    "instance/config.yaml",
    "instance/soul.md",
    "instance/memory/",
    "projects.yaml",
)

# Paths relative to a *project* working directory that must survive.
# Checked when the project IS the koan repo itself.
PROJECT_CORE_PATHS = (
    ".env",
)


def _check_paths(root: Path, paths: tuple) -> Set[str]:
    """Return the set of paths that exist under *root*."""
    present = set()
    for p in paths:
        target = root / p
        if p.endswith("/"):
            if target.is_dir():
                present.add(p)
        else:
            if target.is_file():
                present.add(p)
    return present


def snapshot_core_files(
    koan_root: str,
    project_path: Optional[str] = None,
) -> Set[str]:
    """Take a snapshot of which core files currently exist.

    Returns a set of relative path strings that are present.
    Call this BEFORE a mission runs.
    """
    root = Path(koan_root)
    present = _check_paths(root, CORE_PATHS)

    if project_path:
        proj = Path(project_path)
        for p in PROJECT_CORE_PATHS:
            if (proj / p).is_file():
                present.add(f"project:{p}")

    return present


def check_core_files(
    koan_root: str,
    before: Set[str],
    project_path: Optional[str] = None,
) -> List[str]:
    """Compare current state against a pre-mission snapshot.

    Returns a list of human-readable warnings for files that
    disappeared since *before* was captured.  Empty list = all good.
    """
    after = snapshot_core_files(koan_root, project_path)
    missing = before - after
    if not missing:
        return []

    warnings = []
    for path in sorted(missing):
        if path.startswith("project:"):
            real = path[len("project:"):]
            warnings.append(f"Project file disappeared: {real}")
        else:
            warnings.append(f"Core file disappeared: {path}")
    return warnings


def log_integrity_warnings(warnings: List[str]) -> None:
    """Print integrity warnings to stderr."""
    if not warnings:
        return
    print("[core_files] ⚠️  INTEGRITY CHECK FAILED:", file=sys.stderr)
    for w in warnings:
        print(f"[core_files]   - {w}", file=sys.stderr)
