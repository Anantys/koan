"""Shared module-level state for the messaging bridge.

This module holds configuration and runtime constants that are shared between
awake.py (main loop, chat, outbox) and command_handlers.py (slash commands).
Extracted to avoid circular imports between those two modules.
"""

import os
import sys
from pathlib import Path
from typing import Optional

from app.skills import SkillRegistry, build_registry
from app.utils import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ.get("KOAN_TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("KOAN_TELEGRAM_CHAT_ID", "")
POLL_INTERVAL = int(os.environ.get("KOAN_BRIDGE_INTERVAL", "3"))
CHAT_TIMEOUT = int(os.environ.get("KOAN_CHAT_TIMEOUT", "180"))

KOAN_ROOT = Path(os.environ["KOAN_ROOT"])
INSTANCE_DIR = KOAN_ROOT / "instance"
MISSIONS_FILE = INSTANCE_DIR / "missions.md"
OUTBOX_FILE = INSTANCE_DIR / "outbox.md"


def _migrate_history_file() -> Path:
    """Migrate telegram-history.jsonl to conversation-history.jsonl.

    One-time migration on first import. Idempotent — skips if new file
    already exists. Uses os.rename() which is atomic on POSIX.

    Returns:
        Path to the conversation history file.
    """
    new_path = INSTANCE_DIR / "conversation-history.jsonl"
    old_path = INSTANCE_DIR / "telegram-history.jsonl"

    if old_path.exists() and not new_path.exists():
        try:
            old_path.rename(new_path)
            print(f"[bridge_state] Migrated {old_path.name} → {new_path.name}",
                  file=sys.stderr)
        except OSError as e:
            print(f"[bridge_state] Migration failed ({old_path} → {new_path}): {e}",
                  file=sys.stderr)
            return old_path

    return new_path


CONVERSATION_HISTORY_FILE = _migrate_history_file()
TOPICS_FILE = INSTANCE_DIR / "previous-discussions-topics.json"

def _resolve_default_project_path() -> str:
    """Get the first project's path for CLI cwd fallback."""
    try:
        from app.utils import get_known_projects
        projects = get_known_projects()
        if projects:
            return projects[0][1]
    except Exception as e:
        print(f"[bridge_state] Default project resolution failed: {e}", file=sys.stderr)
    return ""


PROJECT_PATH = _resolve_default_project_path()

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Context loaded once at startup
SOUL = ""
soul_path = INSTANCE_DIR / "soul.md"
if soul_path.exists():
    SOUL = soul_path.read_text()

SUMMARY = ""
summary_path = INSTANCE_DIR / "memory" / "summary.md"
if summary_path.exists():
    SUMMARY = summary_path.read_text()

# Skills registry — cached with mtime-based invalidation.
# Rebuilds automatically when skill directories change on disk
# (e.g., after code deployment adds a new core skill).
_skill_registry: Optional[SkillRegistry] = None
_skill_registry_mtime: float = 0.0


def _skills_dir_mtime() -> float:
    """Get the max mtime of core and instance skills directories.

    When a new skill directory is added or removed, the parent directory's
    mtime changes.  This single stat() call detects structural changes
    without scanning individual SKILL.md files.
    """
    best = 0.0
    # Core skills directory (inside the koan package)
    core_dir = Path(__file__).resolve().parent.parent / "skills" / "core"
    try:
        best = max(best, core_dir.stat().st_mtime)
    except OSError:
        pass
    # Instance skills directory (user-installed skills)
    instance_skills = INSTANCE_DIR / "skills"
    if instance_skills.is_dir():
        try:
            best = max(best, instance_skills.stat().st_mtime)
        except OSError:
            pass
    return best


def _get_registry() -> SkillRegistry:
    """Get the skill registry, rebuilding if skills directories changed."""
    global _skill_registry, _skill_registry_mtime
    current_mtime = _skills_dir_mtime()
    if _skill_registry is None or current_mtime > _skill_registry_mtime:
        extra_dirs = []
        instance_skills = INSTANCE_DIR / "skills"
        if instance_skills.is_dir():
            extra_dirs.append(instance_skills)
        _skill_registry = build_registry(extra_dirs)
        _skill_registry_mtime = current_mtime
    return _skill_registry


def _reset_registry():
    """Reset the registry (for testing and after /skill install/update/remove)."""
    global _skill_registry, _skill_registry_mtime
    _skill_registry = None
    _skill_registry_mtime = 0.0
