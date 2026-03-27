"""Mission spec generator — lightweight spec before complex missions.

Calls Claude CLI with read-only tools to produce a focused spec document
(Goal, Scope, Approach, Out of scope) that anchors implementation and
provides PR reviewer context.

Failures are non-blocking: returns None so the mission proceeds without spec.
"""

import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional


def _slugify(title: str) -> str:
    """Convert a mission title to a filesystem-safe slug.

    Args:
        title: Mission title text.

    Returns:
        Lowercase slug with non-alphanumeric chars replaced by hyphens,
        truncated to 60 chars.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:60]


def _get_spec_timeout() -> int:
    """Get timeout for spec generation (1/4 of skill_timeout)."""
    try:
        from app.config import get_skill_timeout
        return max(60, get_skill_timeout() // 4)
    except (ImportError, OSError, ValueError):
        return 300


def generate_spec(
    project_path: str,
    mission_title: str,
    instance_dir: str,
) -> Optional[str]:
    """Generate a mission spec document via Claude CLI.

    Invokes Claude with read-only tools and a tight turn limit to produce
    a lightweight spec. Failures return None without blocking the mission.

    Args:
        project_path: Path to the target project.
        mission_title: The mission description.
        instance_dir: Path to the instance directory.

    Returns:
        Spec document as a string, or None on failure.
    """
    try:
        from app.cli_provider import run_command
        from app.prompts import load_prompt
    except ImportError as e:
        print(f"[spec_generator] Import error: {e}", file=sys.stderr)
        return None

    try:
        prompt = load_prompt(
            "mission-spec",
            MISSION_TITLE=mission_title,
            PROJECT_PATH=project_path,
        )

        output = run_command(
            prompt,
            project_path,
            allowed_tools=["Read", "Glob", "Grep"],
            max_turns=5,
            timeout=_get_spec_timeout(),
        )

        if not output or not output.strip():
            print("[spec_generator] Empty output from Claude CLI", file=sys.stderr)
            return None

        return output.strip()

    except Exception as e:
        print(f"[spec_generator] Spec generation failed: {e}", file=sys.stderr)
        return None


def save_spec(
    instance_dir: str,
    mission_title: str,
    spec_content: str,
) -> Optional[Path]:
    """Save a spec document to the journal specs directory.

    Writes to journal/{date}/specs/{slug}.md using atomic_write.

    Args:
        instance_dir: Path to the instance directory.
        mission_title: Mission title (used for slug).
        spec_content: The spec document content.

    Returns:
        Path to the saved spec file, or None on failure.
    """
    try:
        from app.utils import atomic_write
    except ImportError:
        return None

    try:
        today = datetime.now().strftime("%Y-%m-%d")
        slug = _slugify(mission_title)
        specs_dir = Path(instance_dir) / "journal" / today / "specs"
        specs_dir.mkdir(parents=True, exist_ok=True)

        spec_path = specs_dir / f"{slug}.md"
        atomic_write(spec_path, spec_content)
        return spec_path

    except Exception as e:
        print(f"[spec_generator] Failed to save spec: {e}", file=sys.stderr)
        return None


def load_spec_for_mission(instance_dir: str, mission_title: str) -> str:
    """Load a previously saved spec for a mission.

    Looks up the spec file by slugified title in today's journal/specs/.

    Args:
        instance_dir: Path to the instance directory.
        mission_title: Mission title to look up.

    Returns:
        Spec content string, or empty string if not found.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    slug = _slugify(mission_title)
    spec_path = Path(instance_dir) / "journal" / today / "specs" / f"{slug}.md"

    try:
        if spec_path.exists():
            return spec_path.read_text().strip()
    except OSError:
        pass

    return ""
