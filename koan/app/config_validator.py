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

from app.config import _VALID_EFFORT_LEVELS
from app.run_log import log

# Top-level keys whose nested contents are validated inline (not via
# SECTION_SCHEMAS), because the sub-key set is open. ``effort`` keys are
# mission types — an open set that grows as new skills land — so each key
# is accepted and only its value (an effort level) is checked.
_INLINE_VALIDATED_NESTED_KEYS = {"effort", "models"}

# Role keys allowed under models.default / models.{provider} / legacy flat models.
_MODEL_ROLE_KEYS = frozenset({
    "mission", "chat", "lightweight", "fallback", "review_mode", "reflect",
})


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
    "startup_delay": "int",
    "fast_reply": "bool",
    "debug": "bool",
    "cli_output_journal": "bool",
    "branch_prefix": "str",
    "skill_timeout": "int",
    "skill_max_turns": "int",
    "analysis_max_turns": "int",
    "reply_max_turns": "int",
    "mission_timeout": "int",
    "bash_foreground_timeout": "int",
    "first_output_timeout": "int",
    "rebase_first_output_timeout": "int",
    "rebase_review_idle_timeout": "int",
    "rebase_review_max_duration": "int",
    "rebase_ci_idle_timeout": "int",
    "rebase_ci_max_duration": "int",
    "rebase_include_bot_feedback": "bool",
    "allow_rebase_foreign_prs": "bool",
    "post_mission_timeout": "int",
    "contemplative_chance": "int",
    "ci_fix_max_attempts": "int",
    "preflight_cache_minutes": "int",
    "spec_complexity_threshold": "int",
    "start_on_pause": "bool",
    "start_passive": "bool",
    "startup_reflection": "bool",
    "auto_pause": "bool",
    "attention_github_notifications": "bool",
    "enable_multiple_instances": "bool",
    "focus": "bool",
    "skip_permissions": "bool",
    "strip_co_authored_by": "bool",
    "cli_provider": "str",
    "mcp": _NESTED,
    "telegram": _NESTED,
    "budget": _NESTED,
    "tools": _NESTED,
    "models": _NESTED,
    "cli": _NESTED,
    "git_auto_merge": _NESTED,
    "github": _NESTED,
    "jira": _NESTED,
    "schedule": _NESTED,
    "logs": _NESTED,
    "local_llm": _NESTED,
    "ollama_launch": _NESTED,
    "usage": _NESTED,
    "email": _NESTED,
    "messaging": _NESTED,
    "auto_update": _NESTED,
    "codex_update": _NESTED,
    "dashboard": _NESTED,
    "notifications": _NESTED,
    "notification_polling": _NESTED,
    "prompt_caching": _NESTED,
    "prompt_guard": _NESTED,
    "plan_review": _NESTED,
    "branch_cleanup": _NESTED,
    "review_concurrency": _NESTED,
    "review_ignore": _NESTED,
    "review_draft_skip": _NESTED,
    "review_pause_label": "str",
    "automation_rules": _NESTED,
    "effort": _NESTED,
    "thinking": _NESTED,
    "stagnation": _NESTED,
    "optimizations": _NESTED,
    "ci_check": _NESTED,
    "running_indicator": _NESTED,
    "verification": _NESTED,
    "config_sync": _NESTED,
    "mission_limits": _NESTED,
}

# Top-level keys that are recognized but deprecated: they still work (honored
# elsewhere for backward compatibility) but should migrate to a new location.
# These must NOT be reported as "unrecognized" — only with their migration hint.
_DEPRECATED_TOP_LEVEL_KEYS: Dict[str, str] = {
    "unlimited_quota": (
        "'unlimited_quota' moved to 'usage.unlimited_quota'; "
        "the top-level form is deprecated"
    ),
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
    # models: validated inline (open nested set) — see _validate_models_section.
    # Supports both legacy flat models.{role} and nested models.default /
    # models.{provider} maps. Leaving this out of SECTION_SCHEMAS alone is
    # not enough: an empty schema would still reject every key.
    # cli: routes each mission role to a provider (flavor or flavor:path).
    # The global form is `cli.default.<role>` plus a single `cli.fallback`;
    # the per-role values live in the `default` mapping so only `default`
    # (dict) and `fallback` (str) appear at the section level. Per-project
    # overrides (flat `cli.<role>`) live in projects.yaml, not here.
    "cli": {
        "default": "dict",
        "fallback": "str",
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
        "reply_authorized_users": "list",
        "reply_rate_limit": "int",
        "natural_language": "bool",
        "subscribe_enabled": "bool",
        "subscribe_max_per_cycle": "int",
        "max_age_hours": "int",
        "stale_drain_hours": "int",
        "check_interval_seconds": "int",
        "max_check_interval_seconds": "int",
        "parallel_workers": "int",
        "review_scan_interval_minutes": "int",
        "mention_scan_interval_minutes": "int",
        "ack_enabled": "bool",
        "max_replies_per_thread_per_hour": "int",
        "webhook": "dict",
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
    "mcp": {
        "enabled": "bool",
        "tools_allow_destructive": "bool",
        "configs": "list",
    },
    "usage": {
        "session_token_limit": "int",
        "weekly_token_limit": "int",
        "budget_mode": "str",
        "unlimited_quota": "bool",
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
    "codex_update": {
        "enabled": "bool",
        "notify": "bool",
    },
    "dashboard": {
        "enabled": "bool",
        "port": "int",
        "nickname": "str",
    },
    "jira": {
        "enabled": "bool",
        "base_url": "str",
        "email": "str",
        "api_token": "str",
        "nickname": "str",
        "commands_enabled": "bool",
        "authorized_users": "list",
        "max_age_hours": "int",
        "check_interval_seconds": "int",
        "max_check_interval_seconds": "int",
        "projects": "dict",
    },
    "notifications": {
        "min_priority": "str",
    },
    "notification_polling": {
        "check_interval_seconds": "int",
        "max_check_interval_seconds": "int",
    },
    "prompt_caching": {
        "same_project_stickiness_percent": "int",
    },
    "prompt_guard": {
        "enabled": "bool",
        "block_mode": "bool",
    },
    "plan_review": {
        "enabled": "bool",
        "max_rounds": "int",
        "implement_gate": "bool",
    },
    "stagnation": {
        "enabled": "bool",
        "check_interval_seconds": "int",
        "abort_after_cycles": "int",
        "sample_lines": "int",
        "max_retry_on_stagnation": "int",
    },
    "verification": {
        "max_requeue": "int",
    },
    # Per-mission cgroup scope (app/mission_scope.py). Size values are strings
    # ("2G") or a bare byte count, so both spellings must validate.
    "mission_limits": {
        "enabled": "bool",
        "memory_reserve": ("str", "int"),
        "memory_min": ("str", "int"),
        "memory_max": ("str", "int"),
    },
    "branch_cleanup": {
        "enabled": "bool",
        "delete_remote_branches": "bool",
        "cleanup_interval_hours": "int",
        "notify_orphans": "bool",
    },
    "review_concurrency": {
        "enabled": "bool",
        "github_workers": "int",
    },
    "review_ignore": {
        "glob": "list",
        "regex": "list",
    },
    "review_draft_skip": {
        "enabled": "bool",
    },
    "automation_rules": {
        "max_fires_per_minute": "int",
    },
    "thinking": {
        "enabled": "bool",
        "budget_tokens": "int",
        "min_mode": "str",
    },
    "optimizations": {
        # Caveman is configured exclusively via the nested mapping
        # ``caveman: {enabled: bool, include: [skill, ...]}``.  Deep
        # validation of that mapping lives in
        # :func:`_validate_caveman_nested` below.
        "caveman": "dict",
        # RTK (https://github.com/rtk-ai/rtk) — optional CLI proxy that
        # compresses common dev-command output before Claude reads it.
        # Configured via ``rtk: {enabled: auto|true|false, awareness: bool,
        # require_jq: bool}``.  Validation lives in
        # :func:`_validate_rtk_nested` below.
        "rtk": "dict",
        "review_compressor": "dict",
        "ponytail": "dict",
    },
    # ci_check also accepts a bare bool shorthand (``ci_check: true``); the
    # dict form carries the toggle plus the queue-safety bounds.
    "ci_check": {
        "enabled": "bool",
        "timeout": "int",
        "max_fix_attempts_per_mission": "int",
        "idle_timeout": "int",
    },
    # running_indicator also accepts a bare bool shorthand
    # (``running_indicator: true``); the dict form carries the sub-toggles.
    "running_indicator": {
        "enabled": "bool",
        "commit_status": "bool",
        "issue_label": "bool",
        "label_name": "str",
    },
    "config_sync": {
        "enabled": "bool",
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

def accepts_non_mapping(key: str, value: Any) -> bool:
    """Whether a ``_NESTED`` key legitimately accepts this non-mapping value.

    Several sections accept a shorthand beside their dict form, and one
    (``mcp``) accepts a legacy list. Both the advisory validator
    (``validate_config``) and the strict startup validator
    (``validate_config_or_raise``) must agree on that set — a value one accepts
    and the other rejects turns a working config into a hard startup stop.
    Keeping the set here, in one place, is what stops them drifting apart.

    Level validation for ``effort`` stays in ``validate_config``: this predicate
    answers "is the SHAPE allowed", not "is the value sane".
    """
    # Legacy ``mcp: [config.json]``. Migrated to ``mcp.configs`` by
    # app.config_migration at startup, still honored by get_mcp_configs().
    if key == "mcp" and isinstance(value, list):
        return True
    # effort accepts a scalar shorthand (effort: "high") applying to every
    # mission, alongside the per-mission-type dict form.
    if key == "effort" and isinstance(value, str):
        return True
    # ci_check accepts a bare bool shorthand — is_ci_check_enabled honors both.
    if key == "ci_check" and isinstance(value, bool):
        return True
    # running_indicator likewise — get_running_indicator_config honors both.
    if key == "running_indicator" and isinstance(value, bool):
        return True
    # stagnation: false is the documented off switch.
    if key == "stagnation" and value is False:
        return True
    return False


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


def _validate_models_section(models: dict) -> List[Tuple[str, str]]:
    """Validate nested or flat ``models:`` configuration.

    Accepts:
    - Nested: ``models.default.{role}``, ``models.{provider}.{role}`` (role
      values must be strings when set).
    - Legacy flat: ``models.{role}`` as a string.

    Provider section names are an open set (claude, grok, codex, …).
    """
    warnings: List[Tuple[str, str]] = []
    for sub_key, sub_value in models.items():
        path = f"models.{sub_key}"
        if sub_value is None:
            continue
        # Nested provider / default block
        if isinstance(sub_value, dict):
            for role, role_val in sub_value.items():
                role_path = f"{path}.{role}"
                if role not in _MODEL_ROLE_KEYS:
                    suggestion = _suggest_typo(role, list(_MODEL_ROLE_KEYS))
                    msg = f"unrecognized key '{role_path}'"
                    if suggestion:
                        msg += f" (did you mean '{path}.{suggestion}'?)"
                    warnings.append((role_path, msg))
                    continue
                if role_val is None:
                    continue
                if not isinstance(role_val, str):
                    warnings.append((
                        role_path,
                        f"'{role_path}' should be str, got {type(role_val).__name__}",
                    ))
            continue
        # Legacy flat role key
        if sub_key in _MODEL_ROLE_KEYS:
            if not isinstance(sub_value, str):
                warnings.append((
                    path,
                    f"'{path}' should be str, got {type(sub_value).__name__}",
                ))
            continue
        # Unknown top-level under models (not a provider dict, not a role)
        suggestion = _suggest_typo(sub_key, list(_MODEL_ROLE_KEYS) + ["default"])
        msg = f"unrecognized key '{path}'"
        if suggestion:
            msg += f" (did you mean 'models.{suggestion}'?)"
        warnings.append((path, msg))
    return warnings


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

    # The 'local' CLI provider has been removed; warn instead of silently
    # falling back to 'claude'.
    if str(config.get("cli_provider", "")).strip().lower() == "local":
        warnings.append((
            "cli_provider",
            "cli_provider: 'local' has been removed and is now ignored "
            "(falling back to 'claude'). To run local models use "
            "'ollama-launch'. See docs/providers/ollama-launch.md.",
        ))

    known_top = list(CONFIG_SCHEMA.keys())

    for deprecated_key, hint in _DEPRECATED_TOP_LEVEL_KEYS.items():
        if deprecated_key in config:
            warnings.append((deprecated_key, hint))

    for key, value in config.items():
        if key in _DEPRECATED_TOP_LEVEL_KEYS:
            # Recognized-but-deprecated: already warned above; do not also
            # flag as unrecognized.
            continue
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
            # Some keys accept a shorthand beside their dict form (e.g.
            # effort: "high" vs effort: {review: low, deep: high}, or the
            # legacy mcp: [config.json] list). accepts_non_mapping owns that
            # set so this validator and validate_config_or_raise stay in step.
            if not isinstance(value, dict):
                if accepts_non_mapping(key, value):
                    # The shape is fine — but still validate effort's LEVEL so a
                    # typo (effort: "hihg") warns instead of silently dropping
                    # the flag, mirroring the per-key validation below.
                    if key == "effort" and isinstance(value, str):
                        if value.strip().lower() not in _VALID_EFFORT_LEVELS:
                            warnings.append((
                                key,
                                f"'effort' invalid effort '{value}' "
                                f"(expected low/medium/high/max)",
                            ))
                    continue
                warnings.append((key, f"'{key}' should be a mapping, got {type(value).__name__}"))
                continue
            # effort: keys are mission types (plan/review/implement/…) plus
            # legacy budget modes (deep/wait) — an open set that grows as new
            # skills land. Accept any key; validate the VALUE is a real effort
            # level (low/medium/high/max, or "" to disable the flag).
            if key == "effort":
                for sub_key, sub_value in value.items():
                    path = f"effort.{sub_key}"
                    if sub_value is None:
                        continue
                    if not isinstance(sub_value, str):
                        warnings.append((
                            path,
                            f"'{path}' should be one of low/medium/high/max, "
                            f"got {type(sub_value).__name__}",
                        ))
                        continue
                    level = sub_value.strip().lower()
                    if level not in _VALID_EFFORT_LEVELS:
                        warnings.append((
                            path,
                            f"'{path}' invalid effort '{sub_value}' "
                            f"(expected low/medium/high/max)",
                        ))
                continue
            # models: open nested set — models.default / models.{provider} maps
            # of role→str, plus legacy flat models.{role} strings.
            if key == "models":
                warnings.extend(_validate_models_section(value))
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

    # Semantic check: deep-validate optimizations.caveman when it's a dict.
    optimizations = config.get("optimizations")
    if isinstance(optimizations, dict):
        caveman = optimizations.get("caveman")
        if isinstance(caveman, dict):
            warnings.extend(_validate_caveman_nested(caveman))
        rtk = optimizations.get("rtk")
        if isinstance(rtk, dict):
            warnings.extend(_validate_rtk_nested(rtk))
        ponytail = optimizations.get("ponytail")
        if isinstance(ponytail, dict):
            warnings.extend(_validate_ponytail_nested(ponytail))
        review_compressor = optimizations.get("review_compressor")
        if isinstance(review_compressor, dict):
            warnings.extend(_validate_review_compressor_nested(review_compressor))

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

    try:
        from app.issue_tracker.config import (
            detect_legacy_jira_projects,
            format_legacy_jira_projects_warning,
        )

        legacy_jira_keys = detect_legacy_jira_projects(config)
        if legacy_jira_keys:
            warnings.append((
                "jira.projects",
                format_legacy_jira_projects_warning(legacy_jira_keys),
            ))
    except ImportError:
        pass

    return warnings


_CAVEMAN_NESTED_SCHEMA: Dict[str, Any] = {
    "enabled": "bool",
    "include": "list",
}


# RTK accepts ``enabled: auto`` (string) in addition to bool, so the schema
# uses a tuple of accepted types.  ``_check_type`` already handles tuples.
_RTK_NESTED_SCHEMA: Dict[str, Any] = {
    "enabled": ("bool", "str"),
    "awareness": "bool",
    "require_jq": "bool",
}


def _validate_rtk_nested(rtk: dict) -> List[Tuple[str, str]]:
    """Validate the nested ``optimizations.rtk`` dict.

    Mirrors :func:`_validate_caveman_nested` with one extra check: when
    ``enabled`` is a string we constrain it to the documented set
    (``auto``, ``true``, ``false``, …) — same set
    :func:`app.config.coerce_rtk_enabled` accepts at runtime, so a typo
    like ``enabld: yse`` surfaces clearly here instead of silently
    falling through to ``auto``.
    """
    from app.config import RTK_ENABLED_VALID

    warnings: List[Tuple[str, str]] = []
    known = list(_RTK_NESTED_SCHEMA.keys())
    for key, value in rtk.items():
        path = f"optimizations.rtk.{key}"
        if key not in _RTK_NESTED_SCHEMA:
            suggestion = _suggest_typo(key, known)
            msg = f"unrecognized key '{path}'"
            if suggestion:
                msg += f" (did you mean 'optimizations.rtk.{suggestion}'?)"
            warnings.append((path, msg))
            continue
        if value is None:
            continue
        expected = _RTK_NESTED_SCHEMA[key]
        if not _check_type(value, expected):
            exp_label = expected if isinstance(expected, str) else "/".join(expected)
            warnings.append((
                path,
                f"'{path}' should be {exp_label}, got {type(value).__name__}",
            ))
            continue
        if key == "enabled" and isinstance(value, str):
            if value.strip().lower() not in RTK_ENABLED_VALID:
                warnings.append((
                    path,
                    f"'{path}' should be one of "
                    f"{sorted(RTK_ENABLED_VALID - {''})}, got {value!r}",
                ))
    return warnings


def _validate_caveman_nested(caveman: dict) -> List[Tuple[str, str]]:
    """Validate the nested ``optimizations.caveman`` dict."""
    warnings: List[Tuple[str, str]] = []
    known = list(_CAVEMAN_NESTED_SCHEMA.keys())
    for key, value in caveman.items():
        path = f"optimizations.caveman.{key}"
        if key not in _CAVEMAN_NESTED_SCHEMA:
            suggestion = _suggest_typo(key, known)
            msg = f"unrecognized key '{path}'"
            if suggestion:
                msg += f" (did you mean 'optimizations.caveman.{suggestion}'?)"
            warnings.append((path, msg))
            continue
        if value is None:
            continue
        expected = _CAVEMAN_NESTED_SCHEMA[key]
        if not _check_type(value, expected):
            exp_label = expected if isinstance(expected, str) else "/".join(expected)
            warnings.append((
                path,
                f"'{path}' should be {exp_label}, got {type(value).__name__}",
            ))
            continue
        if key == "include" and isinstance(value, list):
            for idx, entry in enumerate(value):
                if not isinstance(entry, str):
                    warnings.append((
                        f"{path}[{idx}]",
                        f"'{path}[{idx}]' should be str, got {type(entry).__name__}",
                    ))
    return warnings


_PONYTAIL_NESTED_SCHEMA: Dict[str, Any] = {
    "enabled": "bool",
}


def _validate_ponytail_nested(ponytail: dict) -> List[Tuple[str, str]]:
    """Validate the nested ``optimizations.ponytail`` dict."""
    warnings: List[Tuple[str, str]] = []
    known = list(_PONYTAIL_NESTED_SCHEMA.keys())
    for key, value in ponytail.items():
        path = f"optimizations.ponytail.{key}"
        if key not in _PONYTAIL_NESTED_SCHEMA:
            suggestion = _suggest_typo(key, known)
            msg = f"unrecognized key '{path}'"
            if suggestion:
                msg += f" (did you mean 'optimizations.ponytail.{suggestion}'?)"
            warnings.append((path, msg))
            continue
        if value is None:
            continue
        expected = _PONYTAIL_NESTED_SCHEMA[key]
        if not _check_type(value, expected):
            exp_label = expected if isinstance(expected, str) else "/".join(expected)
            warnings.append((
                path,
                f"'{path}' should be {exp_label}, got {type(value).__name__}",
            ))
    return warnings


_REVIEW_COMPRESSOR_NESTED_SCHEMA: Dict[str, Any] = {
    "enabled": "bool",
}


def _validate_review_compressor_nested(rc: dict) -> List[Tuple[str, str]]:
    """Validate the nested ``optimizations.review_compressor`` dict."""
    warnings: List[Tuple[str, str]] = []
    known = list(_REVIEW_COMPRESSOR_NESTED_SCHEMA.keys())
    for key, value in rc.items():
        path = f"optimizations.review_compressor.{key}"
        if key not in _REVIEW_COMPRESSOR_NESTED_SCHEMA:
            suggestion = _suggest_typo(key, known)
            msg = f"unrecognized key '{path}'"
            if suggestion:
                msg += f" (did you mean 'optimizations.review_compressor.{suggestion}'?)"
            warnings.append((path, msg))
            continue
        if value is None:
            continue
        expected = _REVIEW_COMPRESSOR_NESTED_SCHEMA[key]
        if not _check_type(value, expected):
            exp_label = expected if isinstance(expected, str) else "/".join(expected)
            warnings.append((
                path,
                f"'{path}' should be {exp_label}, got {type(value).__name__}",
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


def find_extra_config_keys(
    koan_root: str,
    user_config: Optional[dict] = None,
) -> List[str]:
    """Report keys present in the user's config but absent from the template.

    Extras usually mean deprecated or removed features — or user typos that
    `validate_config` didn't catch (e.g. misspelled keys nested under dicts).

    Keys that are commented out in the template (e.g. ``# auto_pause: false``
    shown as an opt-in example) are treated as known and not reported — users
    uncommenting such a key should not be told it's a typo.

    Like :func:`detect_config_drift`, parent keys are preferred over children
    when both are missing from the template, to keep reports concise.

    Args:
        koan_root: Path to the koan root directory (where instance.example/ lives).
        user_config: The user's loaded config dict. If None, loads from instance/config.yaml.

    Returns:
        List of extra key paths (dotted notation).
    """
    root = Path(koan_root)
    template_path = root / "instance.example" / "config.yaml"

    if not template_path.exists():
        return []

    try:
        import yaml
        template_text = template_path.read_text()
        template_config = yaml.safe_load(template_text) or {}
    except Exception as e:
        log("warn", f"[config] Could not load template config: {e}")
        return []

    if not isinstance(template_config, dict):
        return []

    # Keys that appear commented-out in the template are documented defaults
    # the user may legitimately uncomment — don't flag them as extras.
    template_commented_keys: Set[str] = _find_commented_keys(template_text)

    if user_config is None:
        user_path = root / "instance" / "config.yaml"
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

    extra = sorted(user_keys - template_keys)

    # Collapse children into their parent when the parent is also extra,
    # and drop keys whose leaf name is commented out in the template.
    filtered = []
    for key in extra:
        parent = key.rsplit(".", 1)[0] if "." in key else None
        if parent and parent in extra:
            continue
        leaf = key.rsplit(".", 1)[-1]
        if leaf in template_commented_keys:
            continue
        filtered.append(key)

    return filtered


def validate_config_or_raise(koan_root: str) -> None:
    """Validate config.yaml strictly — raise ValueError on critical issues.

    Called at startup to fail fast on broken config files. Checks:
    1. YAML is parseable.
    2. Root value is a mapping (dict).
    3. Known section keys have correct types (dict sections aren't scalars).

    Raises:
        ValueError: With a human-readable message describing the issue.
    """
    import yaml as _yaml

    config_path = Path(koan_root) / "instance" / "config.yaml"
    if not config_path.exists():
        return

    try:
        raw = config_path.read_text()
    except OSError as e:
        raise ValueError(f"Cannot read config.yaml: {e}") from e

    try:
        data = _yaml.safe_load(raw)
    except _yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML in config.yaml: {e}") from e

    if data is None:
        return

    if not isinstance(data, dict):
        raise ValueError(
            f"config.yaml root must be a mapping, got {type(data).__name__}"
        )

    errors = []
    for key, value in data.items():
        if value is None or key not in CONFIG_SCHEMA:
            continue
        expected = CONFIG_SCHEMA[key]
        if expected == _NESTED:
            # Shared with validate_config so a shorthand accepted there can
            # never be a hard startup stop here.
            if accepts_non_mapping(key, value):
                continue
            if not isinstance(value, dict):
                errors.append(
                    f"'{key}' must be a mapping, got {type(value).__name__}"
                )
        else:
            if not _check_type(value, expected):
                exp_label = expected if isinstance(expected, str) else "/".join(expected)
                errors.append(
                    f"'{key}' must be {exp_label}, got {type(value).__name__}"
                )

    if errors:
        raise ValueError(
            "config.yaml has invalid entries:\n  - " + "\n  - ".join(errors)
        )


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
