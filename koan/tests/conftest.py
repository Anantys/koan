"""Shared fixtures for koan tests."""

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch):
    """Ensure tests don't touch real instance/ or send real Telegram messages."""
    monkeypatch.setenv("KOAN_TELEGRAM_TOKEN", "fake-token")
    monkeypatch.setenv("KOAN_TELEGRAM_CHAT_ID", "123456")
    monkeypatch.delenv("KOAN_PROJECTS", raising=False)
    # Prevent host CLI provider env vars from leaking into tests
    monkeypatch.delenv("CLI_PROVIDER", raising=False)
    monkeypatch.delenv("KOAN_CLI_PROVIDER", raising=False)
    # Reset projects_merged module-level cache so parallel workers don't
    # see stale project lists from a prior test's KOAN_ROOT.
    try:
        import app.projects_merged as pm
        pm._cached_projects = None
        pm._cached_root = None
        pm._cached_yaml_mtime = None
        pm._cached_workspace_mtime = None
    except Exception:
        pass


@pytest.fixture
def instance_dir(tmp_path):
    """Create a minimal instance directory structure."""
    inst = tmp_path / "instance"
    inst.mkdir()
    (inst / "soul.md").write_text("# Test Soul")
    (inst / "memory").mkdir()
    (inst / "memory" / "summary.md").write_text("Test summary.")
    (inst / "journal").mkdir()
    (inst / "outbox.md").write_text("")
    missions = inst / "missions.md"
    missions.write_text(
        "# Missions\n\n"
        "## Pending\n\n"
        "(none)\n\n"
        "## In Progress\n\n"
        "## Done\n\n"
    )
    return inst
