"""Tests for dashboard agent state parser and SSE stream."""

import json
import time

import pytest
from unittest.mock import patch

from app import dashboard


# ---------------------------------------------------------------------------
# get_agent_state — state derivation from signal files
# ---------------------------------------------------------------------------

class TestGetAgentState:
    """Test get_agent_state() with various combinations of signal files."""

    def test_idle_no_files(self, tmp_path):
        with patch.object(dashboard, "KOAN_ROOT", tmp_path):
            state = dashboard.get_agent_state()
        assert state["state"] == "idle"
        assert state["badge_color"] == "muted"
        assert state["label"] == "Idle"

    def test_stopped(self, tmp_path):
        (tmp_path / ".koan-stop").write_text("1")
        with patch.object(dashboard, "KOAN_ROOT", tmp_path):
            state = dashboard.get_agent_state()
        assert state["state"] == "stopped"
        assert state["badge_color"] == "red"
        assert "Stopped" in state["label"]

    def test_paused_quota(self, tmp_path):
        (tmp_path / ".koan-pause").write_text("quota\n1740000000\nResets at 15:30\n")
        with patch.object(dashboard, "KOAN_ROOT", tmp_path):
            state = dashboard.get_agent_state()
        assert state["state"] == "paused"
        assert state["badge_color"] == "orange"
        assert "quota" in state["label"]
        assert "Resets at 15:30" in state["label"]
        assert state["pause_reason"] == "quota"

    def test_paused_manual(self, tmp_path):
        (tmp_path / ".koan-pause").write_text("manual\n")
        with patch.object(dashboard, "KOAN_ROOT", tmp_path):
            state = dashboard.get_agent_state()
        assert state["state"] == "paused"
        assert state["pause_reason"] == "manual"

    def test_paused_quota_reset_file_only(self, tmp_path):
        """quota_paused via .koan-quota-reset without .koan-pause."""
        (tmp_path / ".koan-quota-reset").write_text("1")
        with patch.object(dashboard, "KOAN_ROOT", tmp_path):
            state = dashboard.get_agent_state()
        assert state["state"] == "paused"
        assert "quota" in state["label"]

    def test_working_executing(self, tmp_path):
        status_file = tmp_path / ".koan-status"
        status_file.write_text("Run 3/10 — executing mission on koan")
        with patch.object(dashboard, "KOAN_ROOT", tmp_path):
            state = dashboard.get_agent_state()
        assert state["state"] == "working"
        assert state["badge_color"] == "green"
        assert state["run_info"] == "3/10"

    def test_working_skill_dispatch(self, tmp_path):
        (tmp_path / ".koan-status").write_text("Run 5/10 — skill dispatch on myproject")
        with patch.object(dashboard, "KOAN_ROOT", tmp_path):
            state = dashboard.get_agent_state()
        assert state["state"] == "working"
        assert state["run_info"] == "5/10"

    def test_working_review_mode(self, tmp_path):
        (tmp_path / ".koan-status").write_text("Run 2/10 — REVIEW on koan")
        with patch.object(dashboard, "KOAN_ROOT", tmp_path):
            state = dashboard.get_agent_state()
        assert state["state"] == "working"
        assert state["autonomous_mode"] == "REVIEW"

    def test_working_implement_mode(self, tmp_path):
        (tmp_path / ".koan-status").write_text("Run 7/10 — IMPLEMENT on backend")
        with patch.object(dashboard, "KOAN_ROOT", tmp_path):
            state = dashboard.get_agent_state()
        assert state["state"] == "working"
        assert state["autonomous_mode"] == "IMPLEMENT"

    def test_working_deep_mode(self, tmp_path):
        (tmp_path / ".koan-status").write_text("Run 1/5 — DEEP on koan")
        with patch.object(dashboard, "KOAN_ROOT", tmp_path):
            state = dashboard.get_agent_state()
        assert state["state"] == "working"
        assert state["autonomous_mode"] == "DEEP"

    def test_working_preparing(self, tmp_path):
        (tmp_path / ".koan-status").write_text("Run 1/10 — preparing")
        with patch.object(dashboard, "KOAN_ROOT", tmp_path):
            state = dashboard.get_agent_state()
        assert state["state"] == "working"

    def test_working_finalizing(self, tmp_path):
        (tmp_path / ".koan-status").write_text("Run 3/10 — finalizing")
        with patch.object(dashboard, "KOAN_ROOT", tmp_path):
            state = dashboard.get_agent_state()
        assert state["state"] == "working"

    def test_sleeping(self, tmp_path):
        (tmp_path / ".koan-status").write_text("Idle — sleeping 300s")
        with patch.object(dashboard, "KOAN_ROOT", tmp_path):
            state = dashboard.get_agent_state()
        assert state["state"] == "sleeping"
        assert state["badge_color"] == "blue"

    def test_contemplating(self, tmp_path):
        (tmp_path / ".koan-status").write_text("Idle — post-contemplation sleep (14:30)")
        with patch.object(dashboard, "KOAN_ROOT", tmp_path):
            state = dashboard.get_agent_state()
        assert state["state"] == "contemplating"
        assert state["badge_color"] == "blue"

    def test_error_recovery(self, tmp_path):
        (tmp_path / ".koan-status").write_text("Error recovery (2/5)")
        with patch.object(dashboard, "KOAN_ROOT", tmp_path):
            state = dashboard.get_agent_state()
        assert state["state"] == "error_recovery"
        assert state["badge_color"] == "red"

    def test_paused_from_status_text(self, tmp_path):
        (tmp_path / ".koan-status").write_text("Paused (1740000000)")
        with patch.object(dashboard, "KOAN_ROOT", tmp_path):
            state = dashboard.get_agent_state()
        assert state["state"] == "paused"

    def test_project_from_file(self, tmp_path):
        (tmp_path / ".koan-project").write_text("myproject")
        (tmp_path / ".koan-status").write_text("Run 1/5 — executing mission on myproject")
        with patch.object(dashboard, "KOAN_ROOT", tmp_path):
            state = dashboard.get_agent_state()
        assert state["project"] == "myproject"

    def test_project_from_status_text(self, tmp_path):
        """If no .koan-project, extract from status text."""
        (tmp_path / ".koan-status").write_text("Run 3/10 — executing mission on koan")
        with patch.object(dashboard, "KOAN_ROOT", tmp_path):
            state = dashboard.get_agent_state()
        assert state["project"] == "koan"

    def test_empty_status_file(self, tmp_path):
        (tmp_path / ".koan-status").write_text("")
        with patch.object(dashboard, "KOAN_ROOT", tmp_path):
            state = dashboard.get_agent_state()
        assert state["state"] == "idle"

    def test_stale_status(self, tmp_path):
        """Status file older than 5 minutes should show idle (stale)."""
        status_file = tmp_path / ".koan-status"
        status_file.write_text("Run 3/10 — executing mission on koan")
        # Backdate the file by 10 minutes
        old_mtime = time.time() - 600
        import os
        os.utime(status_file, (old_mtime, old_mtime))
        with patch.object(dashboard, "KOAN_ROOT", tmp_path):
            state = dashboard.get_agent_state()
        assert state["state"] == "idle"
        assert "stale" in state["label"]

    def test_focus_state(self, tmp_path):
        import json as json_mod
        focus_data = {
            "activated_at": int(time.time()),
            "duration": 18000,
            "reason": "missions",
        }
        (tmp_path / ".koan-focus").write_text(json_mod.dumps(focus_data))
        with patch.object(dashboard, "KOAN_ROOT", tmp_path):
            state = dashboard.get_agent_state()
        assert state["focus"] is not None
        assert "remaining" in state["focus"]
        assert state["focus"]["reason"] == "missions"

    def test_focus_expired(self, tmp_path):
        import json as json_mod
        focus_data = {
            "activated_at": int(time.time()) - 20000,
            "duration": 18000,
            "reason": "missions",
        }
        (tmp_path / ".koan-focus").write_text(json_mod.dumps(focus_data))
        with patch.object(dashboard, "KOAN_ROOT", tmp_path):
            state = dashboard.get_agent_state()
        assert state["focus"] is None


# ---------------------------------------------------------------------------
# Priority: stopped > paused > running > idle
# ---------------------------------------------------------------------------

class TestStatePriority:
    """Verify signal file priority."""

    def test_stopped_overrides_paused(self, tmp_path):
        (tmp_path / ".koan-stop").write_text("1")
        (tmp_path / ".koan-pause").write_text("quota\n1740000000\n")
        (tmp_path / ".koan-status").write_text("Run 3/10 — executing")
        with patch.object(dashboard, "KOAN_ROOT", tmp_path):
            state = dashboard.get_agent_state()
        assert state["state"] == "stopped"

    def test_paused_overrides_running(self, tmp_path):
        (tmp_path / ".koan-pause").write_text("manual\n")
        (tmp_path / ".koan-status").write_text("Run 3/10 — executing")
        with patch.object(dashboard, "KOAN_ROOT", tmp_path):
            state = dashboard.get_agent_state()
        assert state["state"] == "paused"


# ---------------------------------------------------------------------------
# SSE endpoint
# ---------------------------------------------------------------------------

class TestApiStateStream:
    """Test /api/state/stream SSE endpoint."""

    def _make_client(self, tmp_path):
        """Create a test client with patched KOAN_ROOT."""
        from jinja2 import FileSystemLoader
        from pathlib import Path
        import shutil

        inst = tmp_path / "instance"
        inst.mkdir()
        (inst / "memory").mkdir()
        (inst / "journal").mkdir()
        (inst / "missions.md").write_text("# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n\n")

        tpl_src = Path(__file__).parent.parent / "templates"
        tpl_dest = tmp_path / "koan" / "templates"
        shutil.copytree(tpl_src, tpl_dest)

        return inst, tpl_dest

    def test_stream_returns_sse_content_type(self, tmp_path):
        inst, tpl_dest = self._make_client(tmp_path)
        with patch.object(dashboard, "KOAN_ROOT", tmp_path), \
             patch.object(dashboard, "INSTANCE_DIR", inst), \
             patch.object(dashboard, "MISSIONS_FILE", inst / "missions.md"):
            with dashboard.app.test_request_context("/api/state/stream"):
                resp = dashboard.api_state_stream()
        assert resp.content_type == "text/event-stream; charset=utf-8"
        assert resp.headers.get("Cache-Control") == "no-cache"

    def test_stream_emits_valid_json(self, tmp_path):
        (tmp_path / ".koan-status").write_text("Run 1/5 — executing mission on koan")
        inst, _ = self._make_client(tmp_path)
        with patch.object(dashboard, "KOAN_ROOT", tmp_path), \
             patch.object(dashboard, "INSTANCE_DIR", inst), \
             patch.object(dashboard, "MISSIONS_FILE", inst / "missions.md"), \
             patch("app.dashboard.time.sleep", side_effect=RuntimeError("break")):
            resp = dashboard.app.test_client().get("/api/state/stream")
        data_line = None
        for chunk in resp.response:
            if isinstance(chunk, bytes):
                chunk = chunk.decode()
            if chunk.startswith("data: "):
                data_line = chunk
                break
        assert data_line is not None
        payload = json.loads(data_line[6:].strip())
        assert payload["state"] == "working"
        assert payload["run_info"] == "1/5"

    def test_stream_includes_mission_counts(self, tmp_path):
        """SSE payload should include pending/in_progress/done counts."""
        (tmp_path / ".koan-status").write_text("Idle")
        inst, _ = self._make_client(tmp_path)
        (inst / "missions.md").write_text(
            "# Missions\n\n## Pending\n\n- task1\n- task2\n\n"
            "## In Progress\n\n- task3\n\n## Done\n\n- task4\n"
        )
        with patch.object(dashboard, "KOAN_ROOT", tmp_path), \
             patch.object(dashboard, "INSTANCE_DIR", inst), \
             patch.object(dashboard, "MISSIONS_FILE", inst / "missions.md"), \
             patch("app.dashboard.time.sleep", side_effect=RuntimeError("break")):
            resp = dashboard.app.test_client().get("/api/state/stream")
        data_line = None
        for chunk in resp.response:
            if isinstance(chunk, bytes):
                chunk = chunk.decode()
            if chunk.startswith("data: "):
                data_line = chunk
                break
        assert data_line is not None
        payload = json.loads(data_line[6:].strip())
        assert payload["missions"] == {"pending": 2, "in_progress": 1, "done": 1}


# ---------------------------------------------------------------------------
# /api/status includes agent_state
# ---------------------------------------------------------------------------

class TestApiStatusAgentState:
    """Test that /api/status returns agent_state field."""

    def test_api_status_includes_agent_state(self, tmp_path):
        from jinja2 import FileSystemLoader
        from pathlib import Path
        import shutil

        inst = tmp_path / "instance"
        inst.mkdir()
        (inst / "memory").mkdir()
        (inst / "journal").mkdir()
        (inst / "missions.md").write_text(
            "# Missions\n\n## Pending\n\n- task\n\n## In Progress\n\n## Done\n\n"
        )

        tpl_src = Path(__file__).parent.parent / "templates"
        tpl_dest = tmp_path / "koan" / "templates"
        shutil.copytree(tpl_src, tpl_dest)

        (tmp_path / ".koan-status").write_text("Run 2/10 — IMPLEMENT on koan")
        (tmp_path / ".koan-project").write_text("koan")

        with patch.object(dashboard, "KOAN_ROOT", tmp_path), \
             patch.object(dashboard, "INSTANCE_DIR", inst), \
             patch.object(dashboard, "MISSIONS_FILE", inst / "missions.md"):
            dashboard.app.config["TESTING"] = True
            dashboard.app.jinja_loader = FileSystemLoader(str(tpl_dest))
            with dashboard.app.test_client() as client:
                resp = client.get("/api/status")

        data = resp.get_json()
        assert "agent_state" in data
        assert data["agent_state"]["state"] == "working"
        assert data["agent_state"]["project"] == "koan"
        assert data["agent_state"]["autonomous_mode"] == "IMPLEMENT"
        assert data["agent_state"]["run_info"] == "2/10"
        assert data["agent_state"]["badge_color"] == "green"
