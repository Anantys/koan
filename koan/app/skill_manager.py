"""Kōan -- Skill package manager.

Manages external skill sources: install from Git repos, update, remove, and
track installed sources in a skills.yaml manifest.

Manifest format (instance/skills.yaml):
    sources:
      ops:
        url: https://github.com/myorg/koan-skills-ops.git
        ref: main
        installed_at: "2026-02-07T12:00:00"
        updated_at: "2026-02-07T12:00:00"
      analytics:
        url: https://github.com/myorg/koan-skills-analytics.git
        ref: v1.2.0
        installed_at: "2026-02-07T12:00:00"
        updated_at: "2026-02-07T12:00:00"
"""

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

from app.git_utils import run_git as _run_git
from app.utils import atomic_write


@dataclass
class SkillSource:
    """A tracked external skill source (Git repo)."""

    scope: str
    url: str
    ref: str = "main"
    installed_at: str = ""
    updated_at: str = ""


# ---------------------------------------------------------------------------
# Manifest I/O
# ---------------------------------------------------------------------------

_MANIFEST_NAME = "skills.yaml"


def _manifest_path(instance_dir: Path) -> Path:
    return instance_dir / _MANIFEST_NAME


def load_manifest(instance_dir: Path) -> Dict[str, SkillSource]:
    """Load the skills.yaml manifest.

    Returns:
        Dict mapping scope name to SkillSource.
    """
    path = _manifest_path(instance_dir)
    if not path.exists():
        return {}

    content = path.read_text()
    return _parse_manifest(content)


def save_manifest(instance_dir: Path, sources: Dict[str, SkillSource]) -> None:
    """Save the skills.yaml manifest."""
    path = _manifest_path(instance_dir)
    atomic_write(path, _serialize_manifest(sources))


def _parse_manifest(content: str) -> Dict[str, SkillSource]:
    """Parse skills.yaml content into SkillSource dict.

    Uses a minimal YAML-subset parser (no PyYAML dependency).
    """
    sources: Dict[str, SkillSource] = {}

    lines = content.split("\n")
    i = 0
    in_sources = False
    current_scope: Optional[str] = None
    current_data: Dict[str, str] = {}

    while i < len(lines):
        line = lines[i]
        stripped = line.rstrip()

        # Skip empty/comment lines
        if not stripped or stripped.startswith("#"):
            i += 1
            continue

        # Top-level "sources:" key
        if stripped == "sources:":
            in_sources = True
            i += 1
            continue

        if not in_sources:
            i += 1
            continue

        # Scope entry (2-space indent, ends with :)
        scope_match = re.match(r"^  (\w[\w_-]*):\s*$", stripped)
        if scope_match:
            if current_scope and current_data:
                sources[current_scope] = _data_to_source(
                    current_scope, current_data
                )
            current_scope = scope_match.group(1)
            current_data = {}
            i += 1
            continue

        # Field under scope (4-space indent)
        field_match = re.match(r"^    (\w[\w_-]*):\s*(.*)", stripped)
        if field_match and current_scope:
            key = field_match.group(1)
            val = field_match.group(2).strip().strip('"').strip("'")
            current_data[key] = val
            i += 1
            continue

        # Unrecognized line — stop parsing sources block
        if not stripped.startswith(" "):
            if current_scope and current_data:
                sources[current_scope] = _data_to_source(
                    current_scope, current_data
                )
            break

        i += 1

    # Flush last scope
    if current_scope and current_data:
        sources[current_scope] = _data_to_source(current_scope, current_data)

    return sources


def _data_to_source(scope: str, data: Dict[str, str]) -> SkillSource:
    return SkillSource(
        scope=scope,
        url=data.get("url", ""),
        ref=data.get("ref", "main"),
        installed_at=data.get("installed_at", ""),
        updated_at=data.get("updated_at", ""),
    )


def _serialize_manifest(sources: Dict[str, SkillSource]) -> str:
    """Serialize SkillSource dict to skills.yaml content."""
    if not sources:
        return "sources:\n"

    lines = ["sources:"]
    for scope in sorted(sources):
        src = sources[scope]
        lines.append(f"  {scope}:")
        lines.append(f'    url: "{src.url}"')
        lines.append(f'    ref: "{src.ref}"')
        if src.installed_at:
            lines.append(f'    installed_at: "{src.installed_at}"')
        if src.updated_at:
            lines.append(f'    updated_at: "{src.updated_at}"')

    lines.append("")  # trailing newline
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Git operations
# ---------------------------------------------------------------------------

_GIT_URL_RE = re.compile(
    r"^(?:https?://|git@|ssh://)"  # protocol prefix
    r"|\.git$"  # or ends with .git
)

_GITHUB_SHORT_RE = re.compile(r"^[\w.-]+/[\w.-]+$")


def normalize_git_url(url: str) -> str:
    """Normalize a Git URL, expanding GitHub shorthand.

    Examples:
        myorg/koan-skills   -> https://github.com/myorg/koan-skills.git
        https://github.com/myorg/koan-skills -> (unchanged)
        git@github.com:myorg/koan-skills.git -> (unchanged)
    """
    url = url.strip()
    if _GITHUB_SHORT_RE.match(url):
        return f"https://github.com/{url}.git"
    return url


def _extract_scope_from_url(url: str) -> str:
    """Extract a default scope name from a Git URL.

    Examples:
        https://github.com/myorg/koan-skills-ops.git  -> ops
        git@github.com:team/deploy-skills.git          -> deploy-skills
        myorg/koan-skills                              -> koan-skills
    """
    # Strip trailing .git and trailing slash
    clean = url.rstrip("/")
    if clean.endswith(".git"):
        clean = clean[:-4]

    # Get the last path segment
    parts = re.split(r"[/:]", clean)
    name = parts[-1] if parts else "custom"

    # Remove common prefixes
    for prefix in ("koan-skills-", "koan-skill-", "koan-", "skills-"):
        if name.startswith(prefix) and len(name) > len(prefix):
            name = name[len(prefix):]
            break

    return name or "custom"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")




# ---------------------------------------------------------------------------
# Install / Update / Remove
# ---------------------------------------------------------------------------

_RESERVED_SCOPES = frozenset({"core"})


def validate_scope(scope: str) -> Optional[str]:
    """Validate a scope name. Returns error message or None."""
    if not scope:
        return "Scope name cannot be empty."
    if not re.match(r"^[\w][\w-]*$", scope):
        return f"Invalid scope name '{scope}'. Use letters, numbers, hyphens."
    if scope in _RESERVED_SCOPES:
        return f"Scope '{scope}' is reserved."
    return None


def install_skill_source(
    instance_dir: Path,
    url: str,
    scope: Optional[str] = None,
    ref: str = "main",
) -> Tuple[bool, str]:
    """Install a skill source from a Git repository.

    Args:
        instance_dir: Path to instance/ directory.
        url: Git repository URL (or GitHub shorthand like org/repo).
        scope: Scope name (defaults to derived from URL).
        ref: Git ref to checkout (branch, tag, or commit).

    Returns:
        (success, message) tuple.
    """
    url = normalize_git_url(url)

    if scope is None:
        scope = _extract_scope_from_url(url)

    # Validate scope
    err = validate_scope(scope)
    if err:
        return False, err

    # Check if scope already exists
    sources = load_manifest(instance_dir)
    if scope in sources:
        return False, (
            f"Scope '{scope}' already installed from {sources[scope].url}. "
            f"Use /skill update {scope} to update, or "
            f"/skill remove {scope} first."
        )

    skills_dir = instance_dir / "skills"
    target_dir = skills_dir / scope

    if target_dir.exists():
        return False, (
            f"Directory '{scope}' already exists in instance/skills/. "
            f"Remove it first or choose a different scope."
        )

    # Create skills directory if needed
    skills_dir.mkdir(parents=True, exist_ok=True)

    # Clone the repository
    rc, stdout, stderr = _run_git(
        "clone", "--depth", "1", "--branch", ref, url, str(target_dir),
        timeout=60,
    )
    if rc != 0:
        # Try without --branch (ref might be a commit or default branch)
        rc, stdout, stderr = _run_git(
            "clone", "--depth", "1", url, str(target_dir),
            timeout=60,
        )
        if rc != 0:
            return False, f"Git clone failed: {stderr[:300]}"

        # Checkout specific ref if provided and not "main"
        if ref != "main":
            rc, _, stderr = _run_git("checkout", ref, cwd=str(target_dir))
            if rc != 0:
                # Cleanup on failure
                _remove_dir(target_dir)
                return False, f"Git checkout '{ref}' failed: {stderr[:300]}"

    # Count discovered skills
    skill_count = _count_skills_in_dir(target_dir)
    if skill_count == 0:
        _remove_dir(target_dir)
        return False, (
            f"No SKILL.md files found in repository. "
            f"Expected structure: <repo>/<skill-name>/SKILL.md"
        )

    # Update manifest
    now = _now_iso()
    sources[scope] = SkillSource(
        scope=scope, url=url, ref=ref,
        installed_at=now, updated_at=now,
    )
    save_manifest(instance_dir, sources)

    plural = "s" if skill_count != 1 else ""
    return True, (
        f"Installed {skill_count} skill{plural} from {url} "
        f"as scope '{scope}'."
    )


def update_skill_source(
    instance_dir: Path,
    scope: str,
) -> Tuple[bool, str]:
    """Update an installed skill source (git pull).

    Args:
        instance_dir: Path to instance/ directory.
        scope: Scope name to update.

    Returns:
        (success, message) tuple.
    """
    sources = load_manifest(instance_dir)
    if scope not in sources:
        return False, f"Scope '{scope}' not found in installed sources."

    target_dir = instance_dir / "skills" / scope
    if not target_dir.exists():
        return False, (
            f"Directory for scope '{scope}' missing. "
            f"Use /skill remove {scope} then reinstall."
        )

    # Pull latest
    rc, stdout, stderr = _run_git("pull", "--ff-only", cwd=str(target_dir))
    if rc != 0:
        return False, f"Git pull failed for '{scope}': {stderr[:300]}"

    # Check for skill changes
    skill_count = _count_skills_in_dir(target_dir)

    # Update manifest timestamp
    sources[scope].updated_at = _now_iso()
    save_manifest(instance_dir, sources)

    if "Already up to date" in stdout:
        return True, f"'{scope}' already up to date ({skill_count} skills)."

    plural = "s" if skill_count != 1 else ""
    return True, (
        f"Updated '{scope}' ({skill_count} skill{plural}). "
        f"Restart bridge to reload."
    )


def update_all_sources(
    instance_dir: Path,
) -> Tuple[bool, str]:
    """Update all installed skill sources.

    Returns:
        (success, summary_message) tuple.
    """
    sources = load_manifest(instance_dir)
    if not sources:
        return True, "No installed skill sources to update."

    results = []
    any_failure = False
    for scope in sorted(sources):
        ok, msg = update_skill_source(instance_dir, scope)
        results.append(f"{'✅' if ok else '❌'} {scope}: {msg}")
        if not ok:
            any_failure = True

    return not any_failure, "\n".join(results)


def remove_skill_source(
    instance_dir: Path,
    scope: str,
) -> Tuple[bool, str]:
    """Remove an installed skill source.

    Args:
        instance_dir: Path to instance/ directory.
        scope: Scope name to remove.

    Returns:
        (success, message) tuple.
    """
    sources = load_manifest(instance_dir)

    target_dir = instance_dir / "skills" / scope

    if scope not in sources and not target_dir.exists():
        return False, f"Scope '{scope}' not found."

    # Remove directory
    if target_dir.exists():
        _remove_dir(target_dir)

    # Remove from manifest
    if scope in sources:
        del sources[scope]
        save_manifest(instance_dir, sources)

    return True, f"Removed skill source '{scope}'."


def list_sources(instance_dir: Path) -> str:
    """List installed skill sources.

    Returns:
        Formatted string for display.
    """
    sources = load_manifest(instance_dir)

    if not sources:
        return (
            "No external skill sources installed.\n\n"
            "Install with: /skill install <git-url> [scope]"
        )

    lines = ["Installed Skill Sources\n"]
    for scope in sorted(sources):
        src = sources[scope]
        skill_dir = instance_dir / "skills" / scope
        skill_count = _count_skills_in_dir(skill_dir) if skill_dir.exists() else 0
        plural = "s" if skill_count != 1 else ""

        lines.append(f"  {scope} ({skill_count} skill{plural})")
        lines.append(f"    url: {src.url}")
        lines.append(f"    ref: {src.ref}")
        if src.updated_at:
            lines.append(f"    updated: {src.updated_at}")

    lines.append("")
    lines.append("Commands: /skill install|update|remove")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Version utilities
# ---------------------------------------------------------------------------

_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-(.+))?$")


def parse_version(version: str) -> Optional[Tuple[int, int, int, str]]:
    """Parse a semver string into (major, minor, patch, prerelease).

    Returns None if the version string is invalid.
    """
    m = _SEMVER_RE.match(version.strip())
    if not m:
        return None
    return (
        int(m.group(1)),
        int(m.group(2)),
        int(m.group(3)),
        m.group(4) or "",
    )


def compare_versions(a: str, b: str) -> int:
    """Compare two semver strings.

    Returns:
        -1 if a < b, 0 if a == b, 1 if a > b.
        Returns 0 if either version is unparseable.
    """
    pa = parse_version(a)
    pb = parse_version(b)
    if pa is None or pb is None:
        return 0

    # Compare major.minor.patch
    for va, vb in zip(pa[:3], pb[:3]):
        if va < vb:
            return -1
        if va > vb:
            return 1

    # Pre-release: no prerelease > any prerelease (1.0.0 > 1.0.0-alpha)
    if pa[3] and not pb[3]:
        return -1
    if not pa[3] and pb[3]:
        return 1
    if pa[3] < pb[3]:
        return -1
    if pa[3] > pb[3]:
        return 1

    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _count_skills_in_dir(directory: Path) -> int:
    """Count SKILL.md files in a directory tree."""
    if not directory.exists():
        return 0
    return len(list(directory.rglob("SKILL.md")))


def _remove_dir(path: Path) -> None:
    """Remove a directory tree safely."""
    import shutil
    if path.exists() and path.is_dir():
        shutil.rmtree(path)
