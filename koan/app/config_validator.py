"""Startup config.yaml validation.

Checks config keys for known names, validates types, and warns on typos
or unrecognized keys. Called during startup to surface bad config early
instead of silently replacing with defaults.

Also detects config drift: keys present in the template (instance.example/config.yaml)
but missing from the user's config (instance/config.yaml), helping users discover
new features they may not know about.
"""

import difflib
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from app.run_log import log


# ---------------------------------------------------------------------------
# Schema definition
# ---------------------------------------------------------------------------
# Each key maps to its expected type(s) and optional nested schema.
# Types: "int", "bool", "str", "list", "dict"
# A tuple of types means any of those are valid.

_NESTED = "dict"  # marker for nested schema lookup

# Top-level keys
CONFIG_SCHEMA: Dict[str, Any] = {
    "max_runs_per_day": "int",
    "interval_seconds": "int",
    "fast_reply": "bool",
    "debug": "bool",
    "cli_output_journal": "bool",
    "branch_prefix": "str",
    "skill_timeout": "int",
    "mission_timeout": "int",
    "post_mission_timeout": "int",
    "contemplative_chance": "int",
    "start_on_pause": "bool",
    "start_passive": "bool",
    "skip_permissions": "bool",
    "cli_provider": "str",
    "telegram": _NESTED,
    "budget": _NESTED,
    "tools": _NESTED,
    "models": _NESTED,
    "git_auto_merge": _NESTED,
    "github": _NESTED,
    "schedule": _NESTED,
    "logs": _NESTED,
    "local_llm": _NESTED,
    "ollama_launch": _NESTED,
    "usage": _NESTED,
    "email": _NESTED,
    "messaging": _NESTED,
    "auto_update": _NESTED,
    "dashboard": _NESTED,
}

# Sub-schemas for nested sections
SECTION_SCHEMAS: Dict[str, Dict[str, str]] = {
    "telegram": {
        "bot_token": "str",
        "chat_id": "str",
    },
    "budget": {
        "warn_at_percent": "int",
        "stop_at_percent": "int",
    },
    "tools": {
        "chat": ("list", "str"),
        "mission": ("list", "str"),
        "description": "str",
    },
    "models": {
        "mission": "str",
        "chat": "str",
        "lightweight": "str",
        "fallback": "str",
        "review_mode": "str",
    },
    "git_auto_merge": {
        "enabled": "bool",
        "base_branch": "str",
        "strategy": "str",
        "rules": "list",
    },
    "github": {
        "nickname": "str",
        "commands_enabled": "bool",
        "authorized_users": "list",
        "reply_enabled": "bool",
        "max_age_hours": "int",
        "check_interval_seconds": "int",
        "max_check_interval_seconds": "int",
    },
    "schedule": {
        "deep_hours": "str",
        "work_hours": "str",
    },
    "logs": {
        "max_backups": "int",
        "max_size_mb": "int",
        "compress": "bool",
    },
    "local_llm": {
        "base_url": "str",
        "model": "str",
        "api_key": "str",
    },
    "ollama_launch": {
        "model": "str",
    },
    "usage": {
        "session_token_limit": "int",
        "weekly_token_limit": "int",
        "budget_mode": "str",
    },
    "email": {
        "enabled": "bool",
        "max_per_day": "int",
        "require_approval": "bool",
    },
    "messaging": {
        "provider": "str",
    },
    "auto_update": {
        "enabled": "bool",
        "check_interval": "int",
        "notify": "bool",
    },
    "dashboard": {
        "enabled": "bool",
        "port": "int",
    },
}

# Type name → Python type(s) for isinstance checks
_TYPE_MAP = {
    "int": (int,),
    "bool": (bool,),
    "str": (str,),
    "list": (list,),
    "dict": (dict,),
}

# Similarity threshold for typo suggestions
_SIMILARITY_CUTOFF = 0.6


# ---------------------------------------------------------------------------
# Validation logic
# ---------------------------------------------------------------------------

def _check_type(value: Any, expected: Any) -> bool:
    """Check if value matches expected type spec.

    Args:
        value: The config value to check.
        expected: A type string ("int", "bool", etc.) or tuple of type strings.

    Returns:
        True if value matches the expected type.
    """
    if isinstance(expected, tuple):
        return any(_check_type(value, t) for t in expected)
    # YAML bools are Python bools; Python bool is subclass of int,
    # so we need explicit exclusion for int checks.
    if expected == "int" and isinstance(value, bool):
        return False
    py_types = _TYPE_MAP.get(expected, ())
    return isinstance(value, py_types)


def _suggest_typo(key: str, known_keys: list) -> str:
    """Find closest matching key name for typo suggestions."""
    matches = difflib.get_close_matches(key, known_keys, n=1, cutoff=_SIMILARITY_CUTOFF)
    if matches:
        return matches[0]
    return ""


def validate_config(config: dict) -> List[Tuple[str, str]]:
    """Validate a config dict against the known schema.

    Args:
        config: Full config dict (from load_config).

    Returns:
        List of (key_path, warning_message) tuples.
    """
    warnings = []

    if not isinstance(config, dict):
        return [("", "config.yaml root is not a mapping")]

    known_top = list(CONFIG_SCHEMA.keys())

    for key, value in config.items():
        if key not in CONFIG_SCHEMA:
            suggestion = _suggest_typo(key, known_top)
            msg = f"unrecognized key '{key}'"
            if suggestion:
                msg += f" (did you mean '{suggestion}'?)"
            warnings.append((key, msg))
            continue

        expected = CONFIG_SCHEMA[key]

        # Nested section
        if expected == _NESTED:
            if value is None:
                continue
            if not isinstance(value, dict):
                warnings.append((key, f"'{key}' should be a mapping, got {type(value).__name__}"))
                continue
            section_schema = SECTION_SCHEMAS.get(key)
            if section_schema:
                known_sub = list(section_schema.keys())
                for sub_key, sub_value in value.items():
                    path = f"{key}.{sub_key}"
                    if sub_key not in section_schema:
                        suggestion = _suggest_typo(sub_key, known_sub)
                        msg = f"unrecognized key '{path}'"
                        if suggestion:
                            msg += f" (did you mean '{key}.{suggestion}'?)"
                        warnings.append((path, msg))
                        continue
                    if sub_value is None:
                        continue
                    sub_expected = section_schema[sub_key]
                    if not _check_type(sub_value, sub_expected):
                        exp_label = sub_expected if isinstance(sub_expected, str) else "/".join(sub_expected)
                        warnings.append((
                            path,
                            f"'{path}' should be {exp_label}, got {type(sub_value).__name__}",
                        ))
        else:
            # Scalar top-level key
            if value is None:
                continue
            if not _check_type(value, expected):
                exp_label = expected if isinstance(expected, str) else "/".join(expected)
                warnings.append((
                    key,
                    f"'{key}' should be {exp_label}, got {type(value).__name__}",
                ))

    # Semantic check: warn on overlapping deep_hours and work_hours
    schedule = config.get("schedule")
    if isinstance(schedule, dict):
        deep_spec = str(schedule.get("deep_hours", ""))
        work_spec = str(schedule.get("work_hours", ""))
        if deep_spec.strip() and work_spec.strip():
            overlap = _check_schedule_overlap(deep_spec, work_spec)
            if overlap:
                warnings.append((
                    "schedule",
                    f"deep_hours ({deep_spec}) and work_hours ({work_spec}) "
                    f"overlap — deep_hours takes priority in overlapping hours. "
                    f"Recommended: use non-overlapping ranges (e.g., deep_hours: \"0-8\", work_hours: \"8-20\")",
                ))

    return warnings


def _check_schedule_overlap(deep_spec: str, work_spec: str) -> bool:
    """Check if deep_hours and work_hours time ranges overlap.

    Returns True if any hour is covered by both specs.
    """
    try:
        from app.schedule_manager import parse_time_ranges, TimeRange
        deep_ranges = parse_time_ranges(deep_spec)
        work_ranges = parse_time_ranges(work_spec)
    except ValueError:
        return False

    for hour in range(24):
        in_deep = any(r.contains(hour) for r in deep_ranges)
        in_work = any(r.contains(hour) for r in work_ranges)
        if in_deep and in_work:
            return True
    return False


def _collect_keys(d: dict, prefix: str = "") -> set:
    """Recursively collect all key paths from a dict.

    Returns a set of dotted key paths (e.g., {"budget.warn_at_percent", "models.chat"}).
    Top-level keys are returned without prefix. Nested dicts are descended into.
    """
    keys = set()
    for key, value in d.items():
        path = f"{prefix}.{key}" if prefix else key
        keys.add(path)
        if isinstance(value, dict):
            keys.update(_collect_keys(value, path))
    return keys


def _find_commented_keys(text: str) -> Set[str]:
    """Extract key names from commented-out YAML lines.

    Matches lines like "# key_name:" or "#key_name: value" at any indentation.
    Returns the set of key names found (leaf names only, not full paths).
    """
    pattern = re.compile(r"^\s*#\s*(\w+)\s*:", re.MULTILINE)
    return {m.group(1) for m in pattern.finditer(text)}


def detect_config_drift(
    koan_root: str,
    user_config: Optional[dict] = None,
) -> List[str]:
    """Compare user's config.yaml against the template and report missing keys.

    Compares key trees recursively. Reports keys present in the template
    but absent from the user's config as advisory info (not errors).

    Keys that are commented out in the user's config file are excluded from
    the drift report — a commented key means the user is aware of it and
    has chosen to use the default value.

    Args:
        koan_root: Path to the koan root directory (where instance.example/ lives).
        user_config: The user's loaded config dict. If None, loads from instance/config.yaml.

    Returns:
        List of missing key paths (dotted notation, e.g. "auto_update.notify").
    """
    root = Path(koan_root)
    template_path = root / "instance.example" / "config.yaml"

    if not template_path.exists():
        return []

    try:
        import yaml
        template_config = yaml.safe_load(template_path.read_text()) or {}
    except Exception as e:
        log("warn", f"[config] Could not load template config: {e}")
        return []

    if not isinstance(template_config, dict):
        return []

    # Read raw user config text to detect commented-out keys
    user_path = root / "instance" / "config.yaml"
    commented_keys: Set[str] = set()
    if user_path.exists():
        try:
            commented_keys = _find_commented_keys(user_path.read_text())
        except Exception as e:
            log("warn", f"[config] Could not read config for comment detection: {e}")

    if user_config is None:
        if not user_path.exists():
            return []
        try:
            user_config = yaml.safe_load(user_path.read_text()) or {}
        except Exception as e:
            log("warn", f"[config] Could not load user config for drift check: {e}")
            return []

    if not isinstance(user_config, dict):
        return []

    template_keys = _collect_keys(template_config)
    user_keys = _collect_keys(user_config)

    # Keys in template but not in user config
    missing = sorted(template_keys - user_keys)

    # Filter out parent keys whose children are also missing
    # (e.g., if "auto_update" is missing, don't also report "auto_update.enabled")
    # Also filter out keys that are commented out in the user's config file
    filtered = []
    for key in missing:
        parent = key.rsplit(".", 1)[0] if "." in key else None
        if parent and parent in missing:
            continue
        # Check if the leaf key name is commented out in the user's config
        leaf = key.rsplit(".", 1)[-1]
        if leaf in commented_keys:
            continue
        filtered.append(key)

    return filtered


def validate_and_warn(config: dict, koan_root: Optional[str] = None) -> List[str]:
    """Validate config and log warnings. Optionally detect config drift.

    Args:
        config: The loaded config dict.
        koan_root: If provided, also runs config drift detection.

    Returns list of warning messages (for testing).
    """
    warnings = validate_config(config)
    messages = []
    for _path, msg in warnings:
        full_msg = f"[config] {msg}"
        log("warn", full_msg)
        messages.append(full_msg)

    # Config drift detection (advisory only)
    if koan_root:
        missing_keys = detect_config_drift(koan_root, user_config=config)
        if missing_keys:
            keys_list = ", ".join(missing_keys)
            drift_msg = (
                f"[config] Config drift: {len(missing_keys)} key(s) in template "
                f"not in your config.yaml: {keys_list}"
                f" — see instance.example/config.yaml for documentation"
            )
            log("info", drift_msg)
            messages.append(drift_msg)

    return messages
