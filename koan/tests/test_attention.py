"""Tests for the attention zone aggregator (koan/app/attention.py)."""

import json
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import app.attention as attention_module
from app import attention


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_koan_root(tmp_path: Path) -> str:
    """Create a minimal KOAN_ROOT layout under tmp_path."""
    instance = tmp_path / "instance"
    instance.mkdir(parents=True)
    return str(tmp_path)


# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------

class TestMakeId:
    def test_deterministic(self):
        a = attention._make_id("failed-mission", "abc123")
        b = attention._make_id("failed-mission", "abc123")
        assert a == b

    def test_different_parts(self):
        a = attention._make_id("failed-mission", "abc")
        b = attention._make_id("pr", "abc")
        assert a != b

    def test_length(self):
        assert len(attention._make_id("x", "y")) == 16


# ---------------------------------------------------------------------------
# Dismissed items persistence
# ---------------------------------------------------------------------------

class TestDismissedPersistence:
    def test_load_empty(self, tmp_path):
        koan_root = _make_koan_root(tmp_path)
        assert attention.load_dismissed(koan_root) == set()

    def test_save_and_load(self, tmp_path):
        koan_root = _make_koan_root(tmp_path)
        attention.save_dismissed(koan_root, {"id1", "id2"})
        loaded = attention.load_dismissed(koan_root)
        assert loaded == {"id1", "id2"}

    def test_dismiss_item(self, tmp_path):
        koan_root = _make_koan_root(tmp_path)
        attention.dismiss_item(koan_root, "abc")
        assert "abc" in attention.load_dismissed(koan_root)

    def test_dismiss_multiple(self, tmp_path):
        koan_root = _make_koan_root(tmp_path)
        attention.dismiss_item(koan_root, "id1")
        attention.dismiss_item(koan_root, "id2")
        dismissed = attention.load_dismissed(koan_root)
        assert "id1" in dismissed
        assert "id2" in dismissed

    def test_load_corrupt_file(self, tmp_path):
        koan_root = _make_koan_root(tmp_path)
        path = Path(koan_root) / "instance" / ".koan-attention-dismissed.json"
        path.write_text("not json {{{")
        assert attention.load_dismissed(koan_root) == set()


# ---------------------------------------------------------------------------
# Failed missions source
# ---------------------------------------------------------------------------

class TestCollectFailedMissions:
    def test_no_missions_file(self, tmp_path):
        koan_root = _make_koan_root(tmp_path)
        items = attention._collect_failed_missions(koan_root)
        assert items == []

    def test_empty_failed_section(self, tmp_path):
        koan_root = _make_koan_root(tmp_path)
        missions_file = Path(koan_root) / "instance" / "missions.md"
        missions_file.write_text("# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n\n## Failed\n")
        items = attention._collect_failed_missions(koan_root)
        assert items == []

    def test_failed_missions_returned(self, tmp_path):
        koan_root = _make_koan_root(tmp_path)
        missions_file = Path(koan_root) / "instance" / "missions.md"
        missions_file.write_text(
            "# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n\n## Failed\n"
            "- Fix the auth bug ❌ (2024-01-01 12:00)\n"
            "- Update deps ❌ (2024-01-02 09:00)\n"
        )
        items = attention._collect_failed_missions(koan_root)
        assert len(items) == 2
        severities = {i["severity"] for i in items}
        assert severities == {"critical"}
        sources = {i["source"] for i in items}
        assert sources == {"mission"}

    def test_ids_are_deterministic(self, tmp_path):
        koan_root = _make_koan_root(tmp_path)
        missions_file = Path(koan_root) / "instance" / "missions.md"
        content = (
            "# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n\n## Failed\n"
            "- Fix bug ❌ (2024-01-01 12:00)\n"
        )
        missions_file.write_text(content)
        items1 = attention._collect_failed_missions(koan_root)
        items2 = attention._collect_failed_missions(koan_root)
        assert items1[0]["id"] == items2[0]["id"]

    def test_url_links_to_missions_page(self, tmp_path):
        koan_root = _make_koan_root(tmp_path)
        missions_file = Path(koan_root) / "instance" / "missions.md"
        missions_file.write_text(
            "# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n\n## Failed\n"
            "- Some task ❌ (2024-01-01 12:00)\n"
        )
        items = attention._collect_failed_missions(koan_root)
        assert items[0]["url"] == "/missions"


# ---------------------------------------------------------------------------
# Quota source
# ---------------------------------------------------------------------------

class TestCollectQuotaItems:
    def test_no_signal_files(self, tmp_path):
        koan_root = _make_koan_root(tmp_path)
        items = attention._collect_quota_items(koan_root)
        assert items == []

    def test_quota_reset_file(self, tmp_path):
        koan_root = _make_koan_root(tmp_path)
        (Path(koan_root) / ".koan-quota-reset").write_text("1")
        items = attention._collect_quota_items(koan_root)
        assert len(items) == 1
        assert items[0]["severity"] == "warning"
        assert items[0]["source"] == "quota"

    def test_pause_file_non_quota(self, tmp_path):
        koan_root = _make_koan_root(tmp_path)
        # Simulate a non-quota pause — patch get_pause_state
        (Path(koan_root) / ".koan-pause").write_text("manual\n")
        with patch("app.attention.get_attention_items"):  # don't cache
            with patch("app.pause_manager.get_pause_state") as mock_ps:
                mock_ps.return_value = MagicMock(reason="manual")
                items = attention._collect_quota_items(koan_root)
        # No quota item for non-quota pause
        assert items == []


# ---------------------------------------------------------------------------
# PR items source
# ---------------------------------------------------------------------------

class TestCollectPRItems:
    def _pr(self, **kwargs):
        base = {
            "number": 1,
            "title": "Test PR",
            "project": "myproject",
            "url": "https://github.com/org/repo/pull/1",
            "createdAt": "2024-01-01T00:00:00Z",
            "isDraft": False,
            "reviewDecision": None,
            "statusCheckRollup": [],
        }
        base.update(kwargs)
        return base

    def test_no_prs(self, tmp_path):
        koan_root = _make_koan_root(tmp_path)
        with patch("app.pr_tracker.fetch_all_prs", return_value={"prs": []}):
            items = attention._collect_pr_items(koan_root)
        assert items == []

    def test_failing_ci_is_critical(self, tmp_path):
        koan_root = _make_koan_root(tmp_path)
        pr = self._pr(statusCheckRollup=[{"name": "CI", "conclusion": "FAILURE", "state": "COMPLETED"}])
        with patch("app.pr_tracker.fetch_all_prs", return_value={"prs": [pr]}):
            items = attention._collect_pr_items(koan_root)
        assert len(items) == 1
        assert items[0]["severity"] == "critical"
        assert items[0]["source"] == "pr"

    def test_review_required_is_warning(self, tmp_path):
        koan_root = _make_koan_root(tmp_path)
        pr = self._pr(reviewDecision="REVIEW_REQUIRED")
        with patch("app.pr_tracker.fetch_all_prs", return_value={"prs": [pr]}):
            items = attention._collect_pr_items(koan_root)
        assert len(items) == 1
        assert items[0]["severity"] == "warning"

    def test_stale_pr_is_warning(self, tmp_path):
        koan_root = _make_koan_root(tmp_path)
        # 10 days ago
        old_ts = "2020-01-01T00:00:00Z"
        pr = self._pr(createdAt=old_ts)
        with patch("app.pr_tracker.fetch_all_prs", return_value={"prs": [pr]}):
            items = attention._collect_pr_items(koan_root)
        assert len(items) == 1
        assert items[0]["severity"] == "warning"
        assert "stale" in items[0]["title"].lower()

    def test_draft_pr_skipped(self, tmp_path):
        koan_root = _make_koan_root(tmp_path)
        pr = self._pr(isDraft=True, reviewDecision="REVIEW_REQUIRED")
        with patch("app.pr_tracker.fetch_all_prs", return_value={"prs": [pr]}):
            items = attention._collect_pr_items(koan_root)
        assert items == []

    def test_failing_ci_takes_priority_over_review(self, tmp_path):
        koan_root = _make_koan_root(tmp_path)
        pr = self._pr(
            reviewDecision="REVIEW_REQUIRED",
            statusCheckRollup=[{"name": "CI", "conclusion": "FAILURE", "state": "COMPLETED"}],
        )
        with patch("app.pr_tracker.fetch_all_prs", return_value={"prs": [pr]}):
            items = attention._collect_pr_items(koan_root)
        # Only the CI item, not also review
        assert len(items) == 1
        assert items[0]["severity"] == "critical"


# ---------------------------------------------------------------------------
# get_attention_items — aggregator
# ---------------------------------------------------------------------------

class TestGetAttentionItems:
    def setup_method(self):
        # Clear cache before each test
        attention_module._attention_cache = None

    def test_empty_when_no_issues(self, tmp_path):
        koan_root = _make_koan_root(tmp_path)
        missions_file = Path(koan_root) / "instance" / "missions.md"
        missions_file.write_text("# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n\n## Failed\n")
        with patch("app.pr_tracker.fetch_all_prs", return_value={"prs": []}):
            items = attention.get_attention_items(koan_root)
        assert items == []

    def test_dismissed_items_filtered(self, tmp_path):
        koan_root = _make_koan_root(tmp_path)
        missions_file = Path(koan_root) / "instance" / "missions.md"
        missions_file.write_text(
            "# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n\n## Failed\n"
            "- Fix bug ❌ (2024-01-01 12:00)\n"
        )
        with patch("app.pr_tracker.fetch_all_prs", return_value={"prs": []}):
            items = attention.get_attention_items(koan_root)
        assert len(items) == 1
        item_id = items[0]["id"]

        # Dismiss it
        attention.dismiss_item(koan_root, item_id)
        attention_module._attention_cache = None  # bust cache

        with patch("app.pr_tracker.fetch_all_prs", return_value={"prs": []}):
            items2 = attention.get_attention_items(koan_root)
        assert all(i["id"] != item_id for i in items2)

    def test_sorted_by_severity_then_age(self, tmp_path):
        koan_root = _make_koan_root(tmp_path)
        # Quota warning + failed mission (critical)
        missions_file = Path(koan_root) / "instance" / "missions.md"
        missions_file.write_text(
            "# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n\n## Failed\n"
            "- Some task ❌ (2024-01-01 12:00)\n"
        )
        (Path(koan_root) / ".koan-quota-reset").write_text("1")
        with patch("app.pr_tracker.fetch_all_prs", return_value={"prs": []}):
            items = attention.get_attention_items(koan_root)
        # critical should come before warning
        severities = [i["severity"] for i in items]
        critical_idx = next((i for i, s in enumerate(severities) if s == "critical"), None)
        warning_idx = next((i for i, s in enumerate(severities) if s == "warning"), None)
        if critical_idx is not None and warning_idx is not None:
            assert critical_idx < warning_idx

    def test_capped_at_20(self, tmp_path):
        koan_root = _make_koan_root(tmp_path)
        # Create 25 failed missions
        lines = "\n".join(f"- Task {i} ❌ (2024-01-01 12:00)" for i in range(25))
        missions_file = Path(koan_root) / "instance" / "missions.md"
        missions_file.write_text(
            f"# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n\n## Failed\n{lines}\n"
        )
        with patch("app.pr_tracker.fetch_all_prs", return_value={"prs": []}):
            items = attention.get_attention_items(koan_root)
        assert len(items) <= 20

    def test_github_disabled_by_default(self, tmp_path):
        koan_root = _make_koan_root(tmp_path)
        missions_file = Path(koan_root) / "instance" / "missions.md"
        missions_file.write_text("# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n\n## Failed\n")
        with patch("app.pr_tracker.fetch_all_prs", return_value={"prs": []}):
            with patch("app.github_notifications.fetch_unread_notifications") as mock_fetch:
                attention.get_attention_items(koan_root)
                # Should not be called unless config flag is set
                mock_fetch.assert_not_called()

    def test_github_auth_error_handled_gracefully(self, tmp_path):
        koan_root = _make_koan_root(tmp_path)
        missions_file = Path(koan_root) / "instance" / "missions.md"
        missions_file.write_text("# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n\n## Failed\n")
        with patch("app.pr_tracker.fetch_all_prs", return_value={"prs": []}):
            with patch("app.utils.load_config", return_value={"attention_github_notifications": True}):
                with patch("app.github_notifications.fetch_unread_notifications",
                           side_effect=RuntimeError("no auth")):
                    # Should not raise
                    items = attention.get_attention_items(koan_root)
        assert isinstance(items, list)


# ---------------------------------------------------------------------------
# Dashboard API routes (integration)
# ---------------------------------------------------------------------------

class TestAttentionRoutes:
    @pytest.fixture
    def client(self, tmp_path):
        (tmp_path / "instance").mkdir(parents=True)
        from app import dashboard as dash_mod
        dash_mod.app.config["TESTING"] = True
        with patch.object(dash_mod, "KOAN_ROOT", tmp_path):
            with dash_mod.app.test_client() as c:
                yield c, str(tmp_path)

    def test_get_attention_empty(self, client):
        c, koan_root = client
        missions_file = Path(koan_root) / "instance" / "missions.md"
        missions_file.write_text("# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n\n## Failed\n")
        attention_module._attention_cache = None
        with patch("app.pr_tracker.fetch_all_prs", return_value={"prs": []}):
            resp = c.get("/api/attention")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "items" in data
        assert isinstance(data["items"], list)

    def test_dismiss_missing_id(self, client):
        c, _ = client
        resp = c.post("/api/attention/dismiss",
                      data=json.dumps({}),
                      content_type="application/json")
        assert resp.status_code == 400

    def test_dismiss_valid_id(self, client):
        c, koan_root = client
        resp = c.post("/api/attention/dismiss",
                      data=json.dumps({"id": "test123"}),
                      content_type="application/json")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert "test123" in attention.load_dismissed(koan_root)
