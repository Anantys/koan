"""Tests for koan/app/automation_rules.py"""

import pytest

from app.automation_rules import (
    AutomationRule,
    KNOWN_EVENTS,
    KNOWN_ACTIONS,
    add_rule,
    load_rules,
    remove_rule,
    save_rules,
    toggle_rule,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def instance_dir(tmp_path):
    inst = tmp_path / "instance"
    inst.mkdir()
    return str(inst)


# ---------------------------------------------------------------------------
# Load / Save round-trip
# ---------------------------------------------------------------------------


class TestLoadSaveRoundTrip:
    def test_missing_file_returns_empty_list(self, instance_dir):
        rules = load_rules(instance_dir)
        assert rules == []

    def test_empty_yaml_returns_empty_list(self, instance_dir, tmp_path):
        from pathlib import Path
        path = Path(instance_dir) / "automation_rules.yaml"
        path.write_text("")
        rules = load_rules(instance_dir)
        assert rules == []

    def test_add_then_load_round_trips(self, instance_dir):
        rule = add_rule(instance_dir, "post_mission", "notify", {"message": "done"})
        loaded = load_rules(instance_dir)
        assert len(loaded) == 1
        assert loaded[0].id == rule.id
        assert loaded[0].event == "post_mission"
        assert loaded[0].action == "notify"
        assert loaded[0].params["message"] == "done"
        assert loaded[0].enabled is True
        assert loaded[0].created != ""

    def test_save_then_load_multiple_rules(self, instance_dir):
        rules = [
            AutomationRule(id="aaa", event="pre_mission", action="pause", created="2026-01-01T00:00:00"),
            AutomationRule(id="bbb", event="post_mission", action="notify", params={"message": "hi"}, created="2026-01-01T00:00:00"),
        ]
        save_rules(instance_dir, rules)
        loaded = load_rules(instance_dir)
        assert len(loaded) == 2
        assert loaded[0].id == "aaa"
        assert loaded[1].id == "bbb"


# ---------------------------------------------------------------------------
# add_rule / remove_rule / toggle_rule
# ---------------------------------------------------------------------------


class TestMutations:
    def test_add_rule_creates_entry(self, instance_dir):
        rule = add_rule(instance_dir, "session_start", "notify")
        assert rule.event == "session_start"
        assert rule.action == "notify"
        assert rule.enabled is True
        assert len(rule.id) == 8

    def test_remove_rule_deletes_it(self, instance_dir):
        rule = add_rule(instance_dir, "post_mission", "notify")
        result = remove_rule(instance_dir, rule.id)
        assert result is True
        assert load_rules(instance_dir) == []

    def test_remove_nonexistent_returns_false(self, instance_dir):
        result = remove_rule(instance_dir, "does_not_exist")
        assert result is False

    def test_toggle_rule_flips_enabled(self, instance_dir):
        rule = add_rule(instance_dir, "post_mission", "notify", enabled=True)
        updated = toggle_rule(instance_dir, rule.id)
        assert updated is not None
        assert updated.enabled is False
        # Toggle back
        updated2 = toggle_rule(instance_dir, rule.id)
        assert updated2.enabled is True

    def test_toggle_rule_set_explicit_value(self, instance_dir):
        rule = add_rule(instance_dir, "post_mission", "notify", enabled=True)
        updated = toggle_rule(instance_dir, rule.id, enabled=False)
        assert updated.enabled is False

    def test_toggle_nonexistent_returns_none(self, instance_dir):
        result = toggle_rule(instance_dir, "no_such_id")
        assert result is None

    def test_add_multiple_rules_all_persist(self, instance_dir):
        add_rule(instance_dir, "pre_mission", "pause")
        add_rule(instance_dir, "post_mission", "notify")
        add_rule(instance_dir, "session_end", "resume")
        rules = load_rules(instance_dir)
        assert len(rules) == 3


# ---------------------------------------------------------------------------
# Unknown event / action skip behavior
# ---------------------------------------------------------------------------


class TestValidation:
    def test_unknown_event_skipped_on_load(self, instance_dir, capsys):
        from pathlib import Path
        import yaml
        path = Path(instance_dir) / "automation_rules.yaml"
        data = [
            {"id": "ok1", "event": "post_mission", "action": "notify", "enabled": True, "created": ""},
            {"id": "bad", "event": "nonexistent_event", "action": "notify", "enabled": True, "created": ""},
        ]
        path.write_text(yaml.dump(data))
        rules = load_rules(instance_dir)
        assert len(rules) == 1
        assert rules[0].id == "ok1"
        captured = capsys.readouterr()
        assert "nonexistent_event" in captured.err

    def test_unknown_action_skipped_on_load(self, instance_dir, capsys):
        from pathlib import Path
        import yaml
        path = Path(instance_dir) / "automation_rules.yaml"
        data = [
            {"id": "ok1", "event": "post_mission", "action": "notify", "enabled": True, "created": ""},
            {"id": "bad", "event": "post_mission", "action": "send_email", "enabled": True, "created": ""},
        ]
        path.write_text(yaml.dump(data))
        rules = load_rules(instance_dir)
        assert len(rules) == 1
        assert rules[0].id == "ok1"

    def test_invalid_yaml_returns_empty(self, instance_dir, capsys):
        from pathlib import Path
        path = Path(instance_dir) / "automation_rules.yaml"
        path.write_text("{ invalid: yaml: data: :")
        rules = load_rules(instance_dir)
        assert rules == []

    def test_non_list_yaml_returns_empty(self, instance_dir):
        from pathlib import Path
        path = Path(instance_dir) / "automation_rules.yaml"
        path.write_text("key: value\n")
        rules = load_rules(instance_dir)
        assert rules == []

    def test_known_events_set(self):
        assert "session_start" in KNOWN_EVENTS
        assert "session_end" in KNOWN_EVENTS
        assert "pre_mission" in KNOWN_EVENTS
        assert "post_mission" in KNOWN_EVENTS

    def test_known_actions_set(self):
        assert "notify" in KNOWN_ACTIONS
        assert "create_mission" in KNOWN_ACTIONS
        assert "pause" in KNOWN_ACTIONS
        assert "resume" in KNOWN_ACTIONS
        assert "auto_merge" in KNOWN_ACTIONS
