"""Tests for the /ci_recovery skill handler."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# Ensure the koan package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skills.core.ci_recovery.handler import handle


@pytest.fixture
def instance_dir(tmp_path):
    d = tmp_path / "instance"
    d.mkdir()
    return d


def _make_ctx(instance_dir):
    return SimpleNamespace(
        instance_dir=instance_dir,
        args="",
    )


class TestCIRecoveryHandler:
    def test_returns_no_active_sessions_when_empty(self, instance_dir):
        ctx = _make_ctx(instance_dir)
        result = handle(ctx)
        assert "No active" in result

    def test_shows_single_tracked_pr(self, instance_dir):
        from app.check_tracker import set_ci_status

        pr_url = "https://github.com/owner/repo/pull/7"
        set_ci_status(instance_dir, pr_url, "fix_dispatched", 1)

        ctx = _make_ctx(instance_dir)
        result = handle(ctx)
        assert pr_url in result
        assert "fix_dispatched" in result
        assert "1" in result

    def test_shows_multiple_tracked_prs(self, instance_dir):
        from app.check_tracker import set_ci_status

        url1 = "https://github.com/owner/repo/pull/7"
        url2 = "https://github.com/owner/repo/pull/8"
        set_ci_status(instance_dir, url1, "fix_dispatched", 1)
        set_ci_status(instance_dir, url2, "escalated", 2)

        ctx = _make_ctx(instance_dir)
        result = handle(ctx)
        assert url1 in result
        assert url2 in result

    def test_does_not_show_prs_without_ci_status(self, instance_dir):
        from app.check_tracker import mark_checked

        url = "https://github.com/owner/repo/pull/5"
        mark_checked(instance_dir, url, "2026-01-01T00:00:00Z")

        ctx = _make_ctx(instance_dir)
        result = handle(ctx)
        assert "No active" in result
