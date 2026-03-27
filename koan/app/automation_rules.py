"""Automation rules — declarative YAML rules executed by koan/app/hooks.py.

Rules are stored in instance/automation_rules.yaml and interpreted at
hook-fire time. Each rule maps an event to an action with optional params.

Schema (YAML):
    - id: "abc123"
      event: "post_mission"
      action: "notify"
      params:
        message: "Mission completed!"
      enabled: true
      created: "2026-01-01T12:00:00"

Supported events: session_start, session_end, pre_mission, post_mission
Supported actions: notify, create_mission, pause, resume, auto_merge
"""

import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from app.utils import atomic_write

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RULES_FILE = "automation_rules.yaml"

KNOWN_EVENTS = frozenset({"session_start", "session_end", "pre_mission", "post_mission"})
KNOWN_ACTIONS = frozenset({"notify", "create_mission", "pause", "resume", "auto_merge"})


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class AutomationRule:
    """A single automation rule."""

    id: str
    event: str
    action: str
    params: Dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    created: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "event": self.event,
            "action": self.action,
            "params": self.params,
            "enabled": self.enabled,
            "created": self.created,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Optional["AutomationRule"]:
        """Parse a rule from a dict, returning None if invalid."""
        event = data.get("event", "")
        if event not in KNOWN_EVENTS:
            print(
                f"[automation_rules] Unknown event '{event}' — skipping rule.",
                file=sys.stderr,
            )
            return None

        action = data.get("action", "")
        if action not in KNOWN_ACTIONS:
            print(
                f"[automation_rules] Unknown action '{action}' — skipping rule.",
                file=sys.stderr,
            )
            return None

        return cls(
            id=str(data.get("id", uuid.uuid4().hex[:8])),
            event=event,
            action=action,
            params=dict(data.get("params") or {}),
            enabled=bool(data.get("enabled", True)),
            created=str(data.get("created", "")),
        )


# ---------------------------------------------------------------------------
# Load / Save
# ---------------------------------------------------------------------------


def _rules_path(instance_dir: str) -> Path:
    return Path(instance_dir) / RULES_FILE


def load_rules(instance_dir: str) -> List[AutomationRule]:
    """Load rules from instance/automation_rules.yaml.

    Returns an empty list if the file is missing or empty.
    Invalid entries (unknown event/action) are skipped with a warning.
    """
    path = _rules_path(instance_dir)
    if not path.exists():
        return []

    try:
        raw = yaml.safe_load(path.read_text()) or []
    except (yaml.YAMLError, OSError) as exc:
        print(f"[automation_rules] Failed to load {path}: {exc}", file=sys.stderr)
        return []

    if not isinstance(raw, list):
        return []

    rules: List[AutomationRule] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        rule = AutomationRule.from_dict(item)
        if rule is not None:
            rules.append(rule)
    return rules


def save_rules(instance_dir: str, rules: List[AutomationRule]) -> None:
    """Persist rules to instance/automation_rules.yaml atomically."""
    path = _rules_path(instance_dir)
    data = [r.to_dict() for r in rules]
    content = yaml.dump(data, default_flow_style=False, allow_unicode=True)
    atomic_write(path, content)


# ---------------------------------------------------------------------------
# Mutation helpers
# ---------------------------------------------------------------------------


def add_rule(
    instance_dir: str,
    event: str,
    action: str,
    params: Optional[Dict[str, Any]] = None,
    enabled: bool = True,
) -> AutomationRule:
    """Create and persist a new rule. Returns the created rule."""
    rules = load_rules(instance_dir)
    rule = AutomationRule(
        id=uuid.uuid4().hex[:8],
        event=event,
        action=action,
        params=params or {},
        enabled=enabled,
        created=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
    )
    rules.append(rule)
    save_rules(instance_dir, rules)
    return rule


def remove_rule(instance_dir: str, rule_id: str) -> bool:
    """Remove a rule by id. Returns True if found and removed."""
    rules = load_rules(instance_dir)
    new_rules = [r for r in rules if r.id != rule_id]
    if len(new_rules) == len(rules):
        return False
    save_rules(instance_dir, new_rules)
    return True


def toggle_rule(instance_dir: str, rule_id: str, enabled: Optional[bool] = None) -> Optional[AutomationRule]:
    """Toggle (or set) a rule's enabled state. Returns updated rule or None."""
    rules = load_rules(instance_dir)
    for rule in rules:
        if rule.id == rule_id:
            rule.enabled = not rule.enabled if enabled is None else enabled
            save_rules(instance_dir, rules)
            return rule
    return None


def update_rule_params(instance_dir: str, rule_id: str, params: Dict[str, Any]) -> Optional[AutomationRule]:
    """Update params of an existing rule. Returns updated rule or None."""
    rules = load_rules(instance_dir)
    for rule in rules:
        if rule.id == rule_id:
            rule.params.update(params)
            save_rules(instance_dir, rules)
            return rule
    return None
