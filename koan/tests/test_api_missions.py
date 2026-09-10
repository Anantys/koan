"""Tests for REST API mission routes."""

import json
import os
import pytest
from unittest.mock import patch

from app.api import create_app
from tests.store_helpers import seed_missions

_TOKEN = "test-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}


@pytest.fixture
def instance_dir(tmp_path):
    inst = tmp_path / "instance"
    inst.mkdir()
    (inst / "missions.md").write_text(
        "# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n"
    )
    return inst


@pytest.fixture
def api_client(tmp_path, instance_dir):
    with patch.dict(os.environ, {"KOAN_API_TOKEN": _TOKEN, "KOAN_ROOT": str(tmp_path)}):
        app = create_app(koan_root=tmp_path, instance_dir=instance_dir)
        app.config["TESTING"] = True
        with app.test_client() as client:
            yield client


class TestCreateMission:
    def test_create_text_mission_returns_202(self, api_client, instance_dir):
        resp = api_client.post(
            "/v1/missions",
            json={"text": "Fix the bug"},
            headers=_AUTH,
        )
        assert resp.status_code == 202
        data = resp.get_json()
        assert "id" in data
        assert data["status"] == "pending"

    def test_create_command_mission(self, api_client, instance_dir):
        resp = api_client.post(
            "/v1/missions",
            json={"command": "/status"},
            headers=_AUTH,
        )
        assert resp.status_code == 202
        data = resp.get_json()
        assert data["status"] == "pending"

    def test_create_mission_writes_to_missions_md(self, api_client, instance_dir):
        api_client.post(
            "/v1/missions",
            json={"text": "Test mission content"},
            headers=_AUTH,
        )
        content = (instance_dir / "missions.md").read_text()
        assert "Test mission content" in content

    def test_create_mission_with_project_tag(self, api_client, instance_dir):
        api_client.post(
            "/v1/missions",
            json={"text": "Fix bug", "project": "my-project"},
            headers=_AUTH,
        )
        content = (instance_dir / "missions.md").read_text()
        assert "[project:my-project]" in content

    def test_create_mission_writes_sidecar(self, api_client, instance_dir):
        resp = api_client.post(
            "/v1/missions",
            json={"text": "Sidecar test"},
            headers=_AUTH,
        )
        mission_id = resp.get_json()["id"]
        sidecar = instance_dir / ".api-missions.json"
        assert sidecar.exists()
        records = json.loads(sidecar.read_text())
        assert any(r["id"] == mission_id for r in records)

    def test_create_mission_missing_body_returns_422(self, api_client):
        resp = api_client.post("/v1/missions", json={}, headers=_AUTH)
        assert resp.status_code == 422
        data = resp.get_json()
        assert data["error"]["code"] == "invalid_request"

    def test_create_mission_unauthenticated_returns_401(self, api_client):
        resp = api_client.post("/v1/missions", json={"text": "test"})
        assert resp.status_code == 401


class TestGetMission:
    def test_get_existing_mission(self, api_client, instance_dir):
        # Create a mission first
        resp = api_client.post(
            "/v1/missions", json={"text": "Mission to get"}, headers=_AUTH
        )
        mission_id = resp.get_json()["id"]

        resp = api_client.get(f"/v1/missions/{mission_id}", headers=_AUTH)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["id"] == mission_id

    def test_get_nonexistent_mission_returns_404(self, api_client):
        resp = api_client.get("/v1/missions/nonexistent-id", headers=_AUTH)
        assert resp.status_code == 404

    def test_get_mission_reconciles_status(self, api_client, instance_dir):
        """When mission moves to in_progress in missions.md, GET reflects it."""
        resp = api_client.post(
            "/v1/missions", json={"text": "Reconcile me"}, headers=_AUTH
        )
        mission_id = resp.get_json()["id"]

        # Read actual content (missions get timestamp-stamped on insert)
        content = (instance_dir / "missions.md").read_text()
        lines = content.splitlines(keepends=True)
        # Find the line containing our mission text
        pending_line = next(
            (ln for ln in lines if "Reconcile me" in ln), None
        )
        assert pending_line is not None, "Mission not found in missions.md"

        # Move it: remove from pending section, add to in_progress section
        content = content.replace(pending_line, "")
        content = content.replace(
            "## In Progress\n\n",
            f"## In Progress\n\n{pending_line}",
        )
        seed_missions(instance_dir, content)

        resp = api_client.get(f"/v1/missions/{mission_id}", headers=_AUTH)
        data = resp.get_json()
        assert data["status"] == "in_progress"


class TestDeleteMission:
    def test_cancel_pending_mission(self, api_client, instance_dir):
        resp = api_client.post(
            "/v1/missions", json={"text": "Cancel me"}, headers=_AUTH
        )
        mission_id = resp.get_json()["id"]

        resp = api_client.delete(f"/v1/missions/{mission_id}", headers=_AUTH)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "removed"

    def test_cancel_removes_from_missions_md(self, api_client, instance_dir):
        resp = api_client.post(
            "/v1/missions", json={"text": "Remove from file"}, headers=_AUTH
        )
        mission_id = resp.get_json()["id"]

        api_client.delete(f"/v1/missions/{mission_id}", headers=_AUTH)
        content = (instance_dir / "missions.md").read_text()
        assert "Remove from file" not in content

    def test_cancel_in_progress_returns_409(self, api_client, instance_dir):
        resp = api_client.post(
            "/v1/missions", json={"text": "In progress one"}, headers=_AUTH
        )
        mission_id = resp.get_json()["id"]

        # Move to in_progress (missions have timestamps, read actual line)
        content = (instance_dir / "missions.md").read_text()
        lines = content.splitlines(keepends=True)
        pending_line = next(
            (ln for ln in lines if "In progress one" in ln), None
        )
        assert pending_line is not None, "Mission not found in missions.md"

        content = content.replace(pending_line, "")
        content = content.replace(
            "## In Progress\n\n",
            f"## In Progress\n\n{pending_line}",
        )
        seed_missions(instance_dir, content)

        resp = api_client.delete(f"/v1/missions/{mission_id}", headers=_AUTH)
        assert resp.status_code == 409

    def test_cancel_nonexistent_returns_404(self, api_client):
        resp = api_client.delete("/v1/missions/no-such-id", headers=_AUTH)
        assert resp.status_code == 404

    def test_cancel_does_not_remove_substring_match(self, api_client, instance_dir):
        """DELETE must use exact matching — not substring. Deleting 'Fix bug'
        must not remove 'Fix bug in auth module'."""
        resp_short = api_client.post(
            "/v1/missions", json={"text": "Fix bug"}, headers=_AUTH
        )
        resp_long = api_client.post(
            "/v1/missions", json={"text": "Fix bug in auth module"}, headers=_AUTH
        )
        short_id = resp_short.get_json()["id"]

        api_client.delete(f"/v1/missions/{short_id}", headers=_AUTH)

        content = (instance_dir / "missions.md").read_text()
        assert "Fix bug in auth module" in content


class TestListMissions:
    def test_list_empty(self, api_client):
        resp = api_client.get("/v1/missions", headers=_AUTH)
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_list_returns_created_missions(self, api_client):
        api_client.post("/v1/missions", json={"text": "Mission A"}, headers=_AUTH)
        api_client.post("/v1/missions", json={"text": "Mission B"}, headers=_AUTH)

        resp = api_client.get("/v1/missions", headers=_AUTH)
        data = resp.get_json()
        assert len(data) == 2

    def test_list_filter_by_status(self, api_client):
        api_client.post("/v1/missions", json={"text": "Pending one"}, headers=_AUTH)

        resp = api_client.get("/v1/missions?status=pending", headers=_AUTH)
        data = resp.get_json()
        assert len(data) == 1

        resp = api_client.get("/v1/missions?status=done", headers=_AUTH)
        assert resp.get_json() == []

    def test_list_filter_by_project(self, api_client):
        api_client.post(
            "/v1/missions",
            json={"text": "For proj", "project": "alpha"},
            headers=_AUTH,
        )
        api_client.post("/v1/missions", json={"text": "No project"}, headers=_AUTH)

        resp = api_client.get("/v1/missions?project=alpha", headers=_AUTH)
        data = resp.get_json()
        assert len(data) == 1
        assert data[0]["project"] == "alpha"


class TestListMissionsStoreBacked:
    """GET /v1/missions must read the mission store, not the API sidecar."""

    def test_list_returns_store_queued_missions_without_sidecar(
        self, api_client, instance_dir
    ):
        # No .api-missions.json on this host: the bug this fixes.
        assert not (instance_dir / ".api-missions.json").exists()
        seed_missions(
            instance_dir,
            "# Missions\n\n## Pending\n\n"
            "- Fix auth bug\n"
            "- Deploy release\n\n"
            "## In Progress\n\n"
            "## Done\n",
        )
        resp = api_client.get("/v1/missions", headers=_AUTH)
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 2
        assert {m["text"] for m in data} == {"Fix auth bug", "Deploy release"}
        assert all(m["status"] == "pending" for m in data)

    def test_list_agrees_with_status_counts(self, api_client, instance_dir):
        seed_missions(
            instance_dir,
            "# Missions\n\n## Pending\n\n- P1\n- P2\n\n"
            "## In Progress\n\n- Running\n\n## Done\n",
        )
        list_resp = api_client.get("/v1/missions", headers=_AUTH).get_json()
        counts = {}
        for m in list_resp:
            counts[m["status"]] = counts.get(m["status"], 0) + 1
        status = api_client.get("/v1/status", headers=_AUTH).get_json()
        assert counts.get("pending") == status["missions"]["pending"] == 2
        assert counts.get("in_progress") == status["missions"]["in_progress"] == 1

    def test_list_filter_by_project_store_backed(self, api_client, instance_dir):
        seed_missions(
            instance_dir,
            "# Missions\n\n## Pending\n\n"
            "- [project:alpha] For proj\n"
            "- No project\n\n"
            "## In Progress\n\n## Done\n",
        )
        resp = api_client.get("/v1/missions?project=alpha", headers=_AUTH)
        data = resp.get_json()
        assert len(data) == 1
        assert data[0]["project"] == "alpha"

    def test_list_created_api_mission_appears_and_drops_sidecar_fields(
        self, api_client, instance_dir
    ):
        # A mission created via the API lives in both the sidecar and the store;
        # the list is store-backed and renders Mission fields only (no result).
        api_client.post(
            "/v1/missions", json={"text": "API queued"}, headers=_AUTH
        )
        resp = api_client.get("/v1/missions", headers=_AUTH)
        data = resp.get_json()
        texts = {m["text"] for m in data}
        assert "API queued" in texts
        assert all("result" not in m and "created" not in m for m in data)

    def test_list_cancelled_api_mission_not_listed(self, api_client, instance_dir):
        # A cancelled API mission is `removed` in the sidecar only; the
        # store-backed list must not resurrect it and `?status` has no removed.
        create = api_client.post(
            "/v1/missions", json={"text": "To cancel"}, headers=_AUTH
        ).get_json()
        api_client.delete(f"/v1/missions/{create['id']}", headers=_AUTH)
        resp = api_client.get("/v1/missions", headers=_AUTH)
        data = resp.get_json()
        assert all(m["text"] != "To cancel" for m in data)

    def test_list_unknown_status_returns_400(self, api_client, instance_dir):
        seed_missions(
            instance_dir,
            "# Missions\n\n## Pending\n\n- Fix auth bug\n\n"
            "## In Progress\n\n## Done\n",
        )
        resp = api_client.get("/v1/missions?status=bogus", headers=_AUTH)
        assert resp.status_code == 400
        assert "error" in resp.get_json()

    def test_list_limit_caps_rows_per_state(self, api_client, instance_dir):
        seed_missions(
            instance_dir,
            "# Missions\n\n## Pending\n\n- P1\n- P2\n- P3\n\n"
            "## In Progress\n\n## Done\n",
        )
        resp = api_client.get("/v1/missions?status=pending&limit=2", headers=_AUTH)
        data = resp.get_json()
        assert len(data) == 2

    def test_list_limit_zero_or_bad_is_unlimited(self, api_client, instance_dir):
        seed_missions(
            instance_dir,
            "# Missions\n\n## Pending\n\n- P1\n- P2\n- P3\n\n"
            "## In Progress\n\n## Done\n",
        )
        for bad in ("0", "-1", "abc"):
            resp = api_client.get(f"/v1/missions?status=pending&limit={bad}", headers=_AUTH)
            assert len(resp.get_json()) == 3


class TestCancelByText:
    def test_cancel_by_text_marks_removed(self, instance_dir):
        from app.api.mission_index import record_mission, cancel_by_text, get_mission
        mid = record_mission(instance_dir, "- Fix bug", None)
        result = cancel_by_text(instance_dir, "- Fix bug")
        assert result is True
        rec = get_mission(instance_dir, mid)
        assert rec["status"] == "removed"

    def test_cancel_by_text_no_match_returns_false(self, instance_dir):
        from app.api.mission_index import cancel_by_text
        result = cancel_by_text(instance_dir, "- Nonexistent mission")
        assert result is False

    def test_cancel_by_text_only_matches_pending(self, instance_dir):
        from app.api.mission_index import record_mission, cancel_mission, cancel_by_text, get_mission
        mid = record_mission(instance_dir, "- Already done", None)
        cancel_mission(instance_dir, mid)
        result = cancel_by_text(instance_dir, "- Already done")
        assert result is False

    def test_cancel_by_text_exact_match_after_strip(self, instance_dir):
        from app.api.mission_index import record_mission, cancel_by_text, get_mission
        mid = record_mission(instance_dir, "- [project:koan] Fix something", "koan")
        result = cancel_by_text(instance_dir, "- [project:koan] Fix something")
        assert result is True
        assert get_mission(instance_dir, mid)["status"] == "removed"

    def test_cancel_by_text_rejects_substring(self, instance_dir):
        from app.api.mission_index import record_mission, cancel_by_text
        record_mission(instance_dir, "- [project:koan] Fix something", "koan")
        result = cancel_by_text(instance_dir, "Fix")
        assert result is False


class TestReconcileSubstringMatch:
    """Reconcile must not confuse missions that share a common prefix."""

    def test_reconcile_rejects_substring_match(self, api_client, instance_dir):
        """A mission 'Fix bug' reconciled against missions.md containing only
        'Fix bug in auth module' must NOT report as present."""
        from app.api.mission_index import record_mission, reconcile

        mid = record_mission(instance_dir, "- Fix bug", None)

        missions_file = instance_dir / "missions.md"
        missions_file.write_text(
            "# Missions\n\n## Pending\n\n"
            "- Fix bug in auth module\n\n"
            "## In Progress\n\n## Done\n"
        )

        rec = reconcile(instance_dir, missions_file, mid)
        assert rec["status"] == "removed"

    def test_stale_outcome_does_not_override_live_mission(self, instance_dir):
        """A requeued mission shares its canonical key with a prior terminal
        run; a stale outcome row must NOT override a fresh pending status."""
        from app.api.mission_index import record_mission, reconcile
        from app.mission_store.aux_stores import OutcomeStore

        # A prior run of this exact mission failed and was recorded.
        OutcomeStore(str(instance_dir)).record(
            "- Retry me", "failed", "timeout", "killed after 1800s")

        mid = record_mission(instance_dir, "- Retry me", None)
        missions_file = instance_dir / "missions.md"
        missions_file.write_text(
            "# Missions\n\n## Pending\n\n- Retry me\n\n"
            "## In Progress\n\n## Done\n"
        )

        rec = reconcile(instance_dir, missions_file, mid)
        assert rec["status"] == "pending"
        assert rec["outcome"] is None

    def test_stale_outcome_does_not_resurrect_removed_mission(self, instance_dir):
        """A pending mission deleted from the queue (never finalized) shares its
        canonical key with an unrelated prior terminal run of the same text; the
        stale outcome must NOT resurrect it from 'removed' to done/failed."""
        from app.api.mission_index import record_mission, reconcile
        from app.mission_store.aux_stores import OutcomeStore

        OutcomeStore(str(instance_dir)).record("- Retry me", "done", None, None)

        mid = record_mission(instance_dir, "- Retry me", None)
        missions_file = instance_dir / "missions.md"
        # Gone from every section: operator deleted a pending row, never ran it.
        missions_file.write_text(
            "# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n")

        rec = reconcile(instance_dir, missions_file, mid)
        assert rec["status"] == "removed"
        assert rec["outcome"] is None


class TestRecordMissionDedup:
    def test_record_mission_dedup_returns_same_id(self, instance_dir):
        from app.api.mission_index import record_mission, list_missions
        id1 = record_mission(instance_dir, "- Fix bug", None)
        id2 = record_mission(instance_dir, "- Fix bug", None)
        assert id1 == id2
        records = list_missions(instance_dir)
        assert len(records) == 1

    def test_record_mission_dedup_different_project_creates_new(self, instance_dir):
        from app.api.mission_index import record_mission, list_missions
        id1 = record_mission(instance_dir, "- Fix bug", "alpha")
        id2 = record_mission(instance_dir, "- Fix bug", "beta")
        assert id1 != id2
        records = list_missions(instance_dir)
        assert len(records) == 2

    def test_record_mission_no_dedup_across_status(self, instance_dir):
        from app.api.mission_index import record_mission, cancel_mission, list_missions
        id1 = record_mission(instance_dir, "- Repeat task", None)
        cancel_mission(instance_dir, id1)
        id2 = record_mission(instance_dir, "- Repeat task", None)
        assert id1 != id2
        records = list_missions(instance_dir)
        assert len(records) == 2


class TestUpdateMissionText:
    def test_update_text_changes_record(self, instance_dir):
        from app.api.mission_index import record_mission, update_mission_text, get_mission
        mid = record_mission(instance_dir, "- Old text", None)
        result = update_mission_text(instance_dir, mid, "- New text")
        assert result is True
        rec = get_mission(instance_dir, mid)
        assert rec["text"] == "- New text"

    def test_update_text_nonexistent_returns_false(self, instance_dir):
        from app.api.mission_index import update_mission_text
        result = update_mission_text(instance_dir, "no-such-id", "- New")
        assert result is False

    def test_update_text_only_updates_pending(self, instance_dir):
        from app.api.mission_index import record_mission, cancel_mission, update_mission_text, get_mission
        mid = record_mission(instance_dir, "- Done mission", None)
        cancel_mission(instance_dir, mid)
        result = update_mission_text(instance_dir, mid, "- Updated")
        assert result is False
        rec = get_mission(instance_dir, mid)
        assert rec["text"] == "- Done mission"


class TestEditMission:
    def test_edit_pending_mission_returns_200(self, api_client, instance_dir):
        resp = api_client.post(
            "/v1/missions", json={"text": "Original text"}, headers=_AUTH
        )
        mission_id = resp.get_json()["id"]

        resp = api_client.patch(
            f"/v1/missions/{mission_id}",
            json={"text": "Updated text"},
            headers=_AUTH,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["id"] == mission_id
        assert data["status"] == "pending"

    def test_edit_updates_missions_md(self, api_client, instance_dir):
        resp = api_client.post(
            "/v1/missions", json={"text": "Before edit"}, headers=_AUTH
        )
        mission_id = resp.get_json()["id"]

        api_client.patch(
            f"/v1/missions/{mission_id}",
            json={"text": "After edit"},
            headers=_AUTH,
        )
        content = (instance_dir / "missions.md").read_text()
        assert "After edit" in content
        assert "Before edit" not in content

    def test_edit_updates_sidecar_index(self, api_client, instance_dir):
        resp = api_client.post(
            "/v1/missions", json={"text": "Sidecar before"}, headers=_AUTH
        )
        mission_id = resp.get_json()["id"]

        api_client.patch(
            f"/v1/missions/{mission_id}",
            json={"text": "Sidecar after"},
            headers=_AUTH,
        )
        resp = api_client.get(f"/v1/missions/{mission_id}", headers=_AUTH)
        data = resp.get_json()
        assert "Sidecar after" in data["text"]

    def test_edit_nonexistent_returns_404(self, api_client):
        resp = api_client.patch(
            "/v1/missions/no-such-id",
            json={"text": "New text"},
            headers=_AUTH,
        )
        assert resp.status_code == 404

    def test_edit_in_progress_returns_409(self, api_client, instance_dir):
        resp = api_client.post(
            "/v1/missions", json={"text": "Moving mission"}, headers=_AUTH
        )
        mission_id = resp.get_json()["id"]

        content = (instance_dir / "missions.md").read_text()
        lines = content.splitlines(keepends=True)
        pending_line = next(ln for ln in lines if "Moving mission" in ln)
        content = content.replace(pending_line, "")
        content = content.replace(
            "## In Progress\n\n", f"## In Progress\n\n{pending_line}"
        )
        seed_missions(instance_dir, content)

        resp = api_client.patch(
            f"/v1/missions/{mission_id}",
            json={"text": "Try to edit"},
            headers=_AUTH,
        )
        assert resp.status_code == 409

    def test_edit_missing_text_returns_422(self, api_client, instance_dir):
        resp = api_client.post(
            "/v1/missions", json={"text": "Some mission"}, headers=_AUTH
        )
        mission_id = resp.get_json()["id"]

        resp = api_client.patch(
            f"/v1/missions/{mission_id}", json={}, headers=_AUTH
        )
        assert resp.status_code == 422

    def test_edit_empty_text_returns_422(self, api_client, instance_dir):
        resp = api_client.post(
            "/v1/missions", json={"text": "Some mission"}, headers=_AUTH
        )
        mission_id = resp.get_json()["id"]

        resp = api_client.patch(
            f"/v1/missions/{mission_id}",
            json={"text": "   "},
            headers=_AUTH,
        )
        assert resp.status_code == 422

    def test_edit_non_string_text_returns_422(self, api_client, instance_dir):
        resp = api_client.post(
            "/v1/missions", json={"text": "Some mission"}, headers=_AUTH
        )
        mission_id = resp.get_json()["id"]

        resp = api_client.patch(
            f"/v1/missions/{mission_id}",
            json={"text": 12345},
            headers=_AUTH,
        )
        assert resp.status_code == 422

    def test_edit_project_mission_preserves_tag(self, api_client, instance_dir):
        resp = api_client.post(
            "/v1/missions",
            json={"text": "Project task", "project": "my-toolkit"},
            headers=_AUTH,
        )
        mission_id = resp.get_json()["id"]

        api_client.patch(
            f"/v1/missions/{mission_id}",
            json={"text": "Updated project task"},
            headers=_AUTH,
        )

        content = (instance_dir / "missions.md").read_text()
        assert "[project:my-toolkit]" in content
        assert "Updated project task" in content

        resp = api_client.get(f"/v1/missions/{mission_id}", headers=_AUTH)
        data = resp.get_json()
        assert data["status"] == "pending"
        assert "Updated project task" in data["text"]

    def test_edit_invalid_json_returns_422(self, api_client, instance_dir):
        resp = api_client.post(
            "/v1/missions", json={"text": "Some mission"}, headers=_AUTH
        )
        mission_id = resp.get_json()["id"]

        resp = api_client.patch(
            f"/v1/missions/{mission_id}",
            data="not json",
            content_type="application/json",
            headers=_AUTH,
        )
        assert resp.status_code == 422

    def test_edit_duplicate_pending_returns_409(self, api_client, instance_dir):
        resp = api_client.post(
            "/v1/missions", json={"text": "Duplicate task"}, headers=_AUTH
        )
        mission_id = resp.get_json()["id"]

        content = (instance_dir / "missions.md").read_text()
        content = content.replace(
            "## In Progress", "- Duplicate task\n\n## In Progress"
        )
        seed_missions(instance_dir, content)

        resp = api_client.patch(
            f"/v1/missions/{mission_id}",
            json={"text": "New text"},
            headers=_AUTH,
        )
        assert resp.status_code == 409
        assert "Ambiguous" in resp.get_json()["error"]["message"]


class TestReorderMission:
    def test_reorder_returns_200(self, api_client, instance_dir):
        api_client.post("/v1/missions", json={"text": "First"}, headers=_AUTH)
        resp_second = api_client.post(
            "/v1/missions", json={"text": "Second"}, headers=_AUTH
        )
        mission_id = resp_second.get_json()["id"]

        resp = api_client.post(
            "/v1/missions/reorder",
            json={"mission_id": mission_id, "target_position": 1},
            headers=_AUTH,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["id"] == mission_id

    def test_reorder_changes_order_in_missions_md(self, api_client, instance_dir):
        api_client.post("/v1/missions", json={"text": "Alpha"}, headers=_AUTH)
        resp_beta = api_client.post(
            "/v1/missions", json={"text": "Beta"}, headers=_AUTH
        )
        beta_id = resp_beta.get_json()["id"]

        api_client.post(
            "/v1/missions/reorder",
            json={"mission_id": beta_id, "target_position": 1},
            headers=_AUTH,
        )
        content = (instance_dir / "missions.md").read_text()
        alpha_pos = content.find("Alpha")
        beta_pos = content.find("Beta")
        assert beta_pos < alpha_pos

    def test_reorder_nonexistent_returns_404(self, api_client):
        resp = api_client.post(
            "/v1/missions/reorder",
            json={"mission_id": "no-such-id", "target_position": 1},
            headers=_AUTH,
        )
        assert resp.status_code == 404

    def test_reorder_in_progress_returns_409(self, api_client, instance_dir):
        resp = api_client.post(
            "/v1/missions", json={"text": "Will move"}, headers=_AUTH
        )
        mission_id = resp.get_json()["id"]

        content = (instance_dir / "missions.md").read_text()
        lines = content.splitlines(keepends=True)
        pending_line = next(ln for ln in lines if "Will move" in ln)
        content = content.replace(pending_line, "")
        content = content.replace(
            "## In Progress\n\n", f"## In Progress\n\n{pending_line}"
        )
        seed_missions(instance_dir, content)

        resp = api_client.post(
            "/v1/missions/reorder",
            json={"mission_id": mission_id, "target_position": 1},
            headers=_AUTH,
        )
        assert resp.status_code == 409

    def test_reorder_missing_fields_returns_422(self, api_client):
        resp = api_client.post(
            "/v1/missions/reorder", json={}, headers=_AUTH
        )
        assert resp.status_code == 422

    def test_reorder_invalid_json_returns_422(self, api_client):
        resp = api_client.post(
            "/v1/missions/reorder",
            data="not json",
            content_type="application/json",
            headers=_AUTH,
        )
        assert resp.status_code == 422

    def test_reorder_invalid_target_returns_422(self, api_client, instance_dir):
        resp = api_client.post(
            "/v1/missions", json={"text": "Only one"}, headers=_AUTH
        )
        mission_id = resp.get_json()["id"]

        resp = api_client.post(
            "/v1/missions/reorder",
            json={"mission_id": mission_id, "target_position": 99},
            headers=_AUTH,
        )
        assert resp.status_code == 422

    def test_reorder_boolean_target_returns_422(self, api_client, instance_dir):
        resp = api_client.post(
            "/v1/missions", json={"text": "Bool test"}, headers=_AUTH
        )
        mission_id = resp.get_json()["id"]

        resp = api_client.post(
            "/v1/missions/reorder",
            json={"mission_id": mission_id, "target_position": True},
            headers=_AUTH,
        )
        assert resp.status_code == 422

    def test_reorder_float_target_returns_422(self, api_client, instance_dir):
        resp = api_client.post(
            "/v1/missions", json={"text": "Float test"}, headers=_AUTH
        )
        mission_id = resp.get_json()["id"]

        resp = api_client.post(
            "/v1/missions/reorder",
            json={"mission_id": mission_id, "target_position": 1.9},
            headers=_AUTH,
        )
        assert resp.status_code == 422

    def test_reorder_duplicate_pending_returns_409(self, api_client, instance_dir):
        resp = api_client.post(
            "/v1/missions", json={"text": "Dup reorder"}, headers=_AUTH
        )
        mission_id = resp.get_json()["id"]

        content = (instance_dir / "missions.md").read_text()
        content = content.replace(
            "## In Progress", "- Dup reorder\n\n## In Progress"
        )
        seed_missions(instance_dir, content)

        resp = api_client.post(
            "/v1/missions/reorder",
            json={"mission_id": mission_id, "target_position": 1},
            headers=_AUTH,
        )
        assert resp.status_code == 409
        assert "Ambiguous" in resp.get_json()["error"]["message"]


class TestStructuredResult:
    def test_review_mission_get_returns_structured_result(self, api_client, instance_dir):
        url = "https://github.com/o/r/pull/5"
        mid = api_client.post("/v1/missions", json={"command": f"/review {url}"},
                              headers=_AUTH).get_json()["id"]
        records = json.loads((instance_dir / ".api-missions.json").read_text())
        stored = next(r["text"] for r in records if r["id"] == mid)
        seed_missions(
            instance_dir,
            f"# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n\n- {stored}\n")
        fdir = instance_dir / ".review-findings"; fdir.mkdir()
        (fdir / "o_r_5.json").write_text(json.dumps({
            "file_comments": [{"file": "a.py", "line_start": 1, "line_end": 1,
                               "severity": "warning", "title": "t", "comment": "c",
                               "code_snippet": ""}],
            "review_summary": {"lgtm": False, "summary": "s", "checklist": []},
        }))
        data = api_client.get(f"/v1/missions/{mid}", headers=_AUTH).get_json()
        assert data["status"] == "done"
        assert data["result_line"] is not None
        assert data["result"]["kind"] == "review"
        assert data["result"]["review_summary"]["lgtm"] is False
        assert data["result"]["file_comments"][0]["severity"] == "warning"

    def test_non_structured_mission_result_is_null(self, api_client, instance_dir):
        mid = api_client.post("/v1/missions", json={"text": "Fix a typo"},
                              headers=_AUTH).get_json()["id"]
        (instance_dir / "missions.md").write_text(
            "# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n")
        data = api_client.get(f"/v1/missions/{mid}", headers=_AUTH).get_json()
        assert data["result"] is None
        assert data["result_ref"] is None

    def test_result_endpoint_returns_full_blob_when_spilled(self, api_client, instance_dir, monkeypatch):
        from app.api import mission_index as mi
        monkeypatch.setattr(mi, "DEFAULT_RESULT_CAP_BYTES", 64)  # force spill
        url = "https://github.com/o/r/pull/8"
        mid = api_client.post("/v1/missions", json={"command": f"/review {url}"},
                              headers=_AUTH).get_json()["id"]
        records = json.loads((instance_dir / ".api-missions.json").read_text())
        stored = next(r["text"] for r in records if r["id"] == mid)
        seed_missions(
            instance_dir,
            f"# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n\n- {stored}\n")
        fdir = instance_dir / ".review-findings"; fdir.mkdir()
        (fdir / "o_r_8.json").write_text(json.dumps({
            "file_comments": [{"file": "a.py", "line_start": i, "line_end": i,
                               "severity": "warning", "title": "t",
                               "comment": "x" * 200, "code_snippet": ""} for i in range(20)],
            "review_summary": {"lgtm": False, "summary": "big", "checklist": []},
        }))
        rec = api_client.get(f"/v1/missions/{mid}", headers=_AUTH).get_json()
        assert rec["result_ref"] is not None
        assert rec["result"]["review_summary"]["summary"] == "big"
        full = api_client.get(f"/v1/missions/{mid}/result", headers=_AUTH).get_json()
        assert len(full["file_comments"]) == 20

    def test_result_endpoint_404_when_no_result(self, api_client, instance_dir):
        mid = api_client.post("/v1/missions", json={"text": "Fix a typo"},
                              headers=_AUTH).get_json()["id"]
        assert api_client.get(f"/v1/missions/{mid}/result", headers=_AUTH).status_code == 404


class TestFindActiveMissionId:
    def test_find_active_mission_id_prefers_in_progress(self, tmp_path):
        from app.api import mission_index as mi
        inst = tmp_path / "instance"
        inst.mkdir()
        id_pending = mi.record_mission(inst, "- [project:koan] Fix the bug", "koan")
        id_running = mi.record_mission(inst, "- Do the thing", None)
        recs = mi._load_index(inst)
        for r in recs:
            if r["id"] == id_running:
                r["status"] = "in_progress"
        mi._save_index(inst, recs)

        # title arriving with project tag already stripped still matches
        assert mi.find_active_mission_id(inst, "Fix the bug") == id_pending
        assert mi.find_active_mission_id(inst, "Do the thing") == id_running
        assert mi.find_active_mission_id(inst, "unknown mission") is None


class TestGetMissionUsage:
    def test_get_mission_returns_aggregated_usage(self, api_client, instance_dir):
        from app.mission_runner import _record_cost_event
        from app.api import mission_index as mi

        mid = mi.record_mission(instance_dir, "- Fix the bug", None)
        recs = mi._load_index(instance_dir)
        recs[0]["status"] = "in_progress"
        mi._save_index(instance_dir, recs)

        tokens = {"model": "opus", "input_tokens": 100, "output_tokens": 20,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 5,
                  "cost_usd": 0.12}
        _record_cost_event(str(instance_dir), "koan", "/tmp/out.json",
                           "implement", "Fix the bug", tokens=tokens)

        resp = api_client.get(f"/v1/missions/{mid}", headers=_AUTH)
        assert resp.status_code == 200
        usage = resp.get_json()["usage"]
        assert usage["input_tokens"] == 100
        assert usage["output_tokens"] == 20
        assert usage["cache_read_input_tokens"] == 5
        assert usage["call_count"] == 1
        assert usage["models"] == ["opus"]
        assert usage["unattributed"]["call_count"] == 0

    def test_get_mission_usage_reports_unattributed(self, api_client, instance_dir):
        from app.cost_tracker import record_usage
        from app.api import mission_index as mi

        mid = mi.record_mission(instance_dir, "- Fix the bug", None)
        # an id-less event whose title matches — attribution gap
        record_usage(instance_dir=instance_dir, project="koan", model="opus",
                     input_tokens=42, output_tokens=9, mission="Fix the bug")

        usage = api_client.get(f"/v1/missions/{mid}", headers=_AUTH).get_json()["usage"]
        assert usage["call_count"] == 0
        assert usage["unattributed"]["call_count"] == 1
        assert usage["unattributed"]["input_tokens"] == 42

    def test_get_mission_usage_zeroed_when_no_calls(self, api_client, instance_dir):
        resp = api_client.post("/v1/missions", json={"text": "no calls yet"}, headers=_AUTH)
        mid = resp.get_json()["id"]
        usage = api_client.get(f"/v1/missions/{mid}", headers=_AUTH).get_json()["usage"]
        assert usage["call_count"] == 0
        assert usage["input_tokens"] == 0
