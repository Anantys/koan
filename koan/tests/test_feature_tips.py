"""Tests for the feature tip system (app.feature_tips)."""

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional
from unittest.mock import patch

import pytest

from app.feature_tips import (
    _format_tip,
    _get_eligible_skills,
    _load_seen,
    _save_seen,
    _TIP_INTERVAL,
    maybe_send_feature_tip,
    pick_tip,
    reset_tip_throttle,
)


# --- Fixtures ---

@dataclass
class FakeCommand:
    name: str
    description: str = ""
    aliases: list = field(default_factory=list)
    usage: str = ""


@dataclass
class FakeSkill:
    name: str
    scope: str
    description: str = ""
    audience: str = "bridge"
    commands: list = field(default_factory=list)


def _make_skill(name, scope="core", audience="bridge", desc="", usage=""):
    cmd = FakeCommand(name=name, description=desc, usage=usage)
    return FakeSkill(name=name, scope=scope, description=desc, audience=audience, commands=[cmd])


class FakeRegistry:
    def __init__(self, skills):
        self._skills = skills

    def list_all(self):
        return self._skills


# --- _load_seen / _save_seen ---

def test_load_seen_missing_file(tmp_path):
    assert _load_seen(tmp_path / "nope.txt") == set()


def test_load_seen_empty_file(tmp_path):
    p = tmp_path / "seen.txt"
    p.write_text("")
    assert _load_seen(p) == set()


def test_load_save_roundtrip(tmp_path):
    p = tmp_path / "seen.txt"
    original = {"status", "plan", "refactor"}
    _save_seen(p, original)
    loaded = _load_seen(p)
    assert loaded == original


# --- _get_eligible_skills ---

def test_filters_non_core():
    skills = [
        _make_skill("foo", scope="custom"),
        _make_skill("bar", scope="core"),
    ]
    registry = FakeRegistry(skills)
    result = _get_eligible_skills(registry)
    assert len(result) == 1
    assert result[0].name == "bar"


def test_filters_agent_audience():
    skills = [
        _make_skill("agent_only", audience="agent"),
        _make_skill("bridge_ok", audience="bridge"),
        _make_skill("hybrid_ok", audience="hybrid"),
    ]
    registry = FakeRegistry(skills)
    result = _get_eligible_skills(registry)
    names = {s.name for s in result}
    assert names == {"bridge_ok", "hybrid_ok"}


def test_filters_no_commands():
    skill = FakeSkill(name="empty", scope="core", audience="bridge", commands=[])
    registry = FakeRegistry([skill])
    assert _get_eligible_skills(registry) == []


# --- _format_tip ---

def test_format_tip_basic():
    skill = _make_skill("status", desc="Show Koan status")
    msg = _format_tip(skill)
    assert "/status" in msg
    assert "Show Koan status" in msg
    assert "Did you know?" in msg


def test_format_tip_with_usage():
    skill = _make_skill("plan", desc="Plan an idea", usage="/plan <idea>")
    msg = _format_tip(skill)
    assert "/plan <idea>" in msg
    assert "Example:" in msg


# --- pick_tip ---

def test_pick_tip_marks_seen(tmp_path):
    skills = [_make_skill("status"), _make_skill("plan")]
    registry = FakeRegistry(skills)

    with patch("app.skills.build_registry", return_value=registry):
        tip = pick_tip(str(tmp_path))

    assert tip is not None
    seen = _load_seen(tmp_path / "seen_tips.txt")
    assert len(seen) == 1


def test_pick_tip_cycles_when_all_seen(tmp_path):
    skills = [_make_skill("status")]
    registry = FakeRegistry(skills)

    # Pre-populate seen with all skills
    _save_seen(tmp_path / "seen_tips.txt", {"status"})

    with patch("app.skills.build_registry", return_value=registry):
        tip = pick_tip(str(tmp_path))

    assert tip is not None
    assert "/status" in tip
    # Seen file should now have just "status" again (reset + re-add)
    seen = _load_seen(tmp_path / "seen_tips.txt")
    assert seen == {"status"}


def test_pick_tip_no_skills(tmp_path):
    registry = FakeRegistry([])
    with patch("app.skills.build_registry", return_value=registry):
        assert pick_tip(str(tmp_path)) is None


def test_pick_tip_avoids_already_seen(tmp_path):
    skills = [_make_skill("status"), _make_skill("plan")]
    registry = FakeRegistry(skills)

    _save_seen(tmp_path / "seen_tips.txt", {"status"})

    with patch("app.skills.build_registry", return_value=registry):
        tip = pick_tip(str(tmp_path))

    assert "/plan" in tip
    seen = _load_seen(tmp_path / "seen_tips.txt")
    assert seen == {"status", "plan"}


# --- maybe_send_feature_tip ---

def test_maybe_send_throttled(tmp_path):
    reset_tip_throttle()
    skills = [_make_skill("status")]
    registry = FakeRegistry(skills)

    with patch("app.skills.build_registry", return_value=registry), \
         patch("app.utils.append_to_outbox") as mock_outbox:
        # First call should send
        assert maybe_send_feature_tip(str(tmp_path)) is True
        assert mock_outbox.call_count == 1

        # Second call should be throttled
        assert maybe_send_feature_tip(str(tmp_path)) is False
        assert mock_outbox.call_count == 1

    reset_tip_throttle()


def test_maybe_send_after_interval(tmp_path):
    reset_tip_throttle()
    skills = [_make_skill("status"), _make_skill("plan")]
    registry = FakeRegistry(skills)

    with patch("app.skills.build_registry", return_value=registry), \
         patch("app.utils.append_to_outbox") as mock_outbox, \
         patch("app.feature_tips.time") as mock_time:
        # Simulate time progression
        mock_time.monotonic.side_effect = [0.0, 0.0 + _TIP_INTERVAL + 1]
        assert maybe_send_feature_tip(str(tmp_path)) is True
        assert maybe_send_feature_tip(str(tmp_path)) is True
        assert mock_outbox.call_count == 2

    reset_tip_throttle()
