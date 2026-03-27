"""Security audit trail — structured JSONL event logging.

Emits structured events to instance/audit/security.jsonl for
security-relevant agent actions: mission lifecycle, git/GitHub
operations, subprocess executions, config changes, and auth events.

Uses append-only writes with fcntl.flock (matching conversation_history.py
pattern) and reuses log_rotation.py for size-based rotation.
"""

import fcntl
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Event type constants
# ---------------------------------------------------------------------------

MISSION_START = "mission_start"
MISSION_COMPLETE = "mission_complete"
MISSION_FAIL = "mission_fail"
GIT_OPERATION = "git_operation"
SUBPROCESS_EXEC = "subprocess_exec"
CONFIG_CHANGE = "config_change"
AUTH_GRANT = "auth_grant"
AUTH_DENY = "auth_deny"

# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------

# Patterns for known secret formats
_SECRET_PATTERNS = [
    re.compile(r"sk-[a-zA-Z0-9_-]{20,}"),        # Anthropic / OpenAI keys
    re.compile(r"ghp_[a-zA-Z0-9]{36,}"),          # GitHub PAT (classic)
    re.compile(r"ghs_[a-zA-Z0-9]{36,}"),          # GitHub App installation token
    re.compile(r"gho_[a-zA-Z0-9]{36,}"),          # GitHub OAuth token
    re.compile(r"github_pat_[a-zA-Z0-9_]{22,}"),  # GitHub fine-grained PAT
    re.compile(r"xoxb-[a-zA-Z0-9-]+"),            # Slack bot token
    re.compile(r"AKIA[0-9A-Z]{16}"),              # AWS access key ID
    re.compile(r"(?:postgres(?:ql)?|mysql)://[^\s'\"]+"),  # DB connection strings
    re.compile(r"-----BEGIN (?:RSA |DSA |EC |OPENSSH )?PRIVATE KEY-----"),  # SSH private key headers
    re.compile(r"(?:Bearer|Basic)\s+[A-Za-z0-9+/=_-]{20,}", re.IGNORECASE),  # Auth headers
]

# Env var names whose values should always be redacted
_SECRET_ENV_VARS = frozenset({
    "KOAN_TELEGRAM_TOKEN",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "AWS_SECRET_ACCESS_KEY",
    "SLACK_TOKEN",
})

_MAX_DETAIL_LEN = 2000


def _redact_secrets(value: str) -> str:
    """Replace known secret patterns in a string with <REDACTED>."""
    result = value
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub("<REDACTED>", result)
    # Redact env var assignments (e.g. ANTHROPIC_API_KEY=sk-...)
    for var_name in _SECRET_ENV_VARS:
        env_val = os.environ.get(var_name, "")
        if env_val and env_val in result:
            result = result.replace(env_val, "<REDACTED>")
    return result


def _redact_list(args: list) -> list:
    """Redact secrets from a list of command arguments."""
    return [_redact_secrets(str(a)) for a in args]


def _truncate(value, max_len: int = _MAX_DETAIL_LEN) -> str:
    """Truncate a value to max_len characters."""
    s = str(value)
    if len(s) > max_len:
        return s[:max_len - 3] + "..."
    return s


# ---------------------------------------------------------------------------
# Audit config
# ---------------------------------------------------------------------------

_DEFAULT_MAX_SIZE_MB = 10


def _get_audit_config() -> dict:
    """Load audit config from config.yaml. Returns defaults on any error."""
    try:
        from app.utils import load_config
        config = load_config()
    except (OSError, ValueError, KeyError):
        config = {}
    audit_cfg = config.get("audit") or {}
    return {
        "enabled": bool(audit_cfg.get("enabled", True)),
        "max_size_mb": int(audit_cfg.get("max_size_mb", _DEFAULT_MAX_SIZE_MB)),
        "redact_patterns": audit_cfg.get("redact_patterns") or [],
    }


# ---------------------------------------------------------------------------
# Core logging
# ---------------------------------------------------------------------------

def _get_audit_path() -> Path:
    """Return path to audit log file, creating directory lazily."""
    koan_root = os.environ.get("KOAN_ROOT", "")
    if not koan_root:
        raise OSError("KOAN_ROOT not set")
    audit_dir = Path(koan_root) / "instance" / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    return audit_dir / "security.jsonl"


def _rotate_if_needed(audit_path: Path, max_size_bytes: int):
    """Rotate audit log if it exceeds the configured size."""
    try:
        if not audit_path.exists():
            return
        if audit_path.stat().st_size < max_size_bytes:
            return
        from app.log_rotation import rotate_log
        rotate_log(audit_path)
    except (OSError, ImportError) as exc:
        print(f"[security_audit] Rotation failed: {exc}", file=sys.stderr)


def log_event(
    event_type: str,
    *,
    actor: Optional[dict] = None,
    details: Optional[dict] = None,
    result: str = "success",
):
    """Append a single audit event to the JSONL log.

    This function never raises — errors are printed to stderr.

    Args:
        event_type: One of the event type constants (MISSION_START, etc.).
        actor: Optional dict with "type" and "id" keys.
        details: Optional dict with event-specific data.
        result: Outcome string (default "success").
    """
    try:
        audit_cfg = _get_audit_config()
        if not audit_cfg["enabled"]:
            return

        audit_path = _get_audit_path()

        # Build event record
        event = {
            "timestamp": datetime.now().isoformat(),
            "event_type": event_type,
            "result": result,
        }
        if actor is not None:
            event["actor"] = actor
        if details is not None:
            # Redact secrets and truncate values in details
            safe_details = {}
            for k, v in details.items():
                if isinstance(v, list):
                    safe_details[k] = _redact_list(v)
                elif isinstance(v, str):
                    safe_details[k] = _truncate(_redact_secrets(v))
                else:
                    safe_details[k] = v
            event["details"] = safe_details

        # Check rotation before appending
        max_size_bytes = audit_cfg["max_size_mb"] * 1024 * 1024
        _rotate_if_needed(audit_path, max_size_bytes)

        # Append with flock (same pattern as conversation_history.py)
        with open(audit_path, "a", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
                f.flush()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    except Exception as exc:
        print(f"[security_audit] Failed to log event: {exc}", file=sys.stderr)


def read_recent_events(count: int = 50) -> list:
    """Read the last N events from the audit log.

    Returns a list of dicts (newest last). Returns [] on any error.
    """
    try:
        audit_path = _get_audit_path()
        if not audit_path.exists():
            return []
        with open(audit_path, "r", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                lines = f.readlines()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        events = []
        for line in lines[-count:]:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return events
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[security_audit] Failed to read events: {exc}", file=sys.stderr)
        return []
