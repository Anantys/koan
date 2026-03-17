"""Startup config.yaml validation.

Checks config keys for known names, validates types, and warns on typos
or unrecognized keys. Called during startup to surface bad config early
instead of silently replacing with defaults.
"""

import difflib
from typing import Any, Dict, List, Tuple

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


def validate_and_warn(config: dict) -> List[str]:
    """Validate config and log warnings.

    Returns list of warning messages (for testing).
    """
    warnings = validate_config(config)
    messages = []
    for _path, msg in warnings:
        full_msg = f"[config] {msg}"
        log("warn", full_msg)
        messages.append(full_msg)
    return messages
