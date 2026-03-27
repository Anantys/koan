"""Tests for koan/dashboard.py"""

import json
import shutil
import subprocess

import pytest
from jinja2 import FileSystemLoader
from pathlib import Path
from unittest.mock import patch, MagicMock

from app import dashboard

REAL_TEMPLATES = Path(__file__).parent.parent / "templates"


@pytest.fixture
def instance_dir(tmp_path):
    """Create a minimal instance directory."""
    inst = tmp_path / "instance"
    inst.mkdir()
    (inst / "memory" / "global").mkdir(parents=True)
    (inst / "memory" / "projects" / "koan").mkdir(parents=True)
    (inst / "journal" / "2026-02-01").mkdir(parents=True)

    (inst / "soul.md").write_text("You are Kōan.")
    (inst / "memory" / "summary.md").write_text("Session 1: bootstrapped.")
    (inst / "missions.md").write_text(
        "# Missions\n\n"
        "## Pending\n\n"
        "- [project:koan] Build dashboard\n"
        "- Fix something\n\n"
        "## In Progress\n\n"
        "### Admin Dashboard\n"
        "- ~~Phase 1~~ done\n"
        "- Phase 2 pending\n\n"
        "## Done\n\n"
        "- ~~Exploration~~ (session 3)\n"
    )
    (inst / "journal" / "2026-02-01" / "koan.md").write_text(
        "## Session 34\nBuilt the dashboard.\n"
    )
    return inst


@pytest.fixture
def app_client(instance_dir, tmp_path):
    """Create a Flask test client with patched paths."""
    # Copy real templates so Flask can render them
    tpl_dest = tmp_path / "koan" / "templates"
    shutil.copytree(REAL_TEMPLATES, tpl_dest)
    with patch.object(dashboard, "INSTANCE_DIR", instance_dir), \
         patch.object(dashboard, "MISSIONS_FILE", instance_dir / "missions.md"), \
         patch.object(dashboard, "OUTBOX_FILE", instance_dir / "outbox.md"), \
         patch.object(dashboard, "SOUL_FILE", instance_dir / "soul.md"), \
         patch.object(dashboard, "SUMMARY_FILE", instance_dir / "memory" / "summary.md"), \
         patch.object(dashboard, "JOURNAL_DIR", instance_dir / "journal"), \
         patch.object(dashboard, "PENDING_FILE", instance_dir / "journal" / "pending.md"), \
         patch.object(dashboard, "KOAN_ROOT", tmp_path):
        dashboard.app.config["TESTING"] = True
        dashboard.app.jinja_loader = FileSystemLoader(str(tpl_dest))
        with dashboard.app.test_client() as client:
            yield client


class TestParsingMissions:
    def test_parse_sections(self, instance_dir):
        with patch.object(dashboard, "MISSIONS_FILE", instance_dir / "missions.md"):
            result = dashboard.parse_missions()
            assert len(result["pending"]) == 2
            assert len(result["in_progress"]) == 1  # complex block
            assert len(result["done"]) == 1

    def test_parse_empty(self, tmp_path):
        with patch.object(dashboard, "MISSIONS_FILE", tmp_path / "nope.md"):
            result = dashboard.parse_missions()
            assert result == {"pending": [], "in_progress": [], "done": []}


class TestRoutes:
    def test_index(self, app_client):
        resp = app_client.get("/")
        assert resp.status_code == 200
        assert b"Dashboard" in resp.data

    def test_missions_page(self, app_client):
        resp = app_client.get("/missions")
        assert resp.status_code == 200
        assert b"Build dashboard" in resp.data

    def test_add_mission(self, app_client, instance_dir):
        with patch.object(dashboard, "MISSIONS_FILE", instance_dir / "missions.md"):
            resp = app_client.post("/missions/add", data={
                "mission": "New test mission",
                "project": "koan",
            }, follow_redirects=True)
            assert resp.status_code == 200
            content = (instance_dir / "missions.md").read_text()
            assert "[project:koan] New test mission" in content

    def test_add_mission_no_project(self, app_client, instance_dir):
        with patch.object(dashboard, "MISSIONS_FILE", instance_dir / "missions.md"):
            resp = app_client.post("/missions/add", data={
                "mission": "Simple mission",
                "project": "",
            }, follow_redirects=True)
            assert resp.status_code == 200
            content = (instance_dir / "missions.md").read_text()
            assert "- Simple mission" in content

    def test_journal_page(self, app_client):
        resp = app_client.get("/journal")
        assert resp.status_code == 200
        assert b"2026-02-01" in resp.data
        assert b"Built the dashboard" in resp.data

    def test_chat_page(self, app_client):
        resp = app_client.get("/chat")
        assert resp.status_code == 200
        assert b"Envoyer" in resp.data

    def test_api_status(self, app_client):
        resp = app_client.get("/api/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["missions"]["pending"] == 2
        assert data["missions"]["in_progress"] == 1

    def test_chat_send_mission(self, app_client, instance_dir):
        with patch.object(dashboard, "MISSIONS_FILE", instance_dir / "missions.md"):
            resp = app_client.post("/chat/send", data={
                "message": "Do something cool",
                "mode": "mission",
            })
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["ok"] is True
            assert data["type"] == "mission"

    def test_chat_send_empty(self, app_client):
        resp = app_client.post("/chat/send", data={
            "message": "",
            "mode": "chat",
        })
        data = resp.get_json()
        assert data["ok"] is False


class TestUsageApi:
    def test_api_usage_exposes_cache_metrics(self, app_client):
        fake_summary = {
            "total_input": 1000,
            "total_output": 500,
            "cache_creation_input_tokens": 300,
            "cache_read_input_tokens": 1200,
            "cache_hit_rate": 0.48,
            "count": 3,
            "by_project": {"koan": {"input_tokens": 1000, "output_tokens": 500, "count": 3}},
            "by_model": {
                "claude-sonnet-4-20250514": {
                    "input_tokens": 1000,
                    "output_tokens": 500,
                    "cache_creation_input_tokens": 300,
                    "cache_read_input_tokens": 1200,
                    "count": 3,
                }
            },
        }
        fake_daily = [{
            "date": "2026-03-21",
            "total_input": 1000,
            "total_output": 500,
            "cache_creation_input_tokens": 300,
            "cache_read_input_tokens": 1200,
            "cache_hit_rate": 0.48,
            "count": 3,
            "cost": 0.12,
        }]

        with patch("app.cost_tracker.summarize_range", return_value=fake_summary), \
             patch("app.cost_tracker.get_pricing_config", return_value={"sonnet": {"input": 3.0, "output": 15.0}}), \
             patch("app.cost_tracker.estimate_cost", return_value=0.12), \
             patch("app.cost_tracker.estimate_cache_savings", return_value=0.00324), \
             patch("app.cost_tracker.daily_series", return_value=fake_daily):
            resp = app_client.get("/api/usage?days=7")

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["cache_creation_input_tokens"] == 300
        assert data["cache_read_input_tokens"] == 1200
        assert data["cache_hit_rate"] == pytest.approx(0.48)
        assert data["estimated_cache_savings"] == pytest.approx(0.00324)
        assert data["daily"][0]["cache_read_input_tokens"] == 1200

    def test_api_usage_without_pricing_returns_null_cache_savings(self, app_client):
        fake_summary = {
            "total_input": 0,
            "total_output": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_hit_rate": 0.0,
            "count": 0,
            "by_project": {},
            "by_model": {},
        }
        with patch("app.cost_tracker.summarize_range", return_value=fake_summary), \
             patch("app.cost_tracker.get_pricing_config", return_value=None), \
             patch("app.cost_tracker.daily_series", return_value=[]):
            resp = app_client.get("/api/usage?days=1")

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["has_pricing"] is False
        assert data["estimated_cache_savings"] is None


class TestSignals:
    def test_no_signals(self, tmp_path):
        with patch.object(dashboard, "KOAN_ROOT", tmp_path):
            status = dashboard.get_signal_status()
            assert status["stop_requested"] is False
            assert status["quota_paused"] is False
            assert status["loop_status"] == ""

    def test_stop_signal(self, tmp_path):
        (tmp_path / ".koan-stop").write_text("STOP")
        with patch.object(dashboard, "KOAN_ROOT", tmp_path):
            status = dashboard.get_signal_status()
            assert status["stop_requested"] is True

    def test_loop_status(self, tmp_path):
        (tmp_path / ".koan-status").write_text("3/20")
        with patch.object(dashboard, "KOAN_ROOT", tmp_path):
            status = dashboard.get_signal_status()
            assert status["loop_status"] == "3/20"


class TestJournal:
    def test_get_entries(self, instance_dir):
        with patch.object(dashboard, "JOURNAL_DIR", instance_dir / "journal"):
            entries = dashboard.get_journal_entries(limit=7)
            assert len(entries) == 1
            assert entries[0]["date"] == "2026-02-01"
            assert entries[0]["entries"][0]["project"] == "koan"

    def test_empty_journal(self, tmp_path):
        with patch.object(dashboard, "JOURNAL_DIR", tmp_path / "journal"):
            entries = dashboard.get_journal_entries()
            assert entries == []


class TestChatSend:
    """Test /chat/send endpoint — the Claude chat handler."""

    @patch("app.dashboard.get_allowed_tools", return_value="")
    @patch("app.dashboard.get_tools_description", return_value="")
    @patch("app.dashboard.save_conversation_message")
    @patch("app.dashboard.load_recent_history", return_value=[])
    @patch("app.dashboard.format_conversation_history", return_value="")
    @patch("app.dashboard.subprocess.run")
    def test_chat_success(self, mock_run, mock_fmt, mock_hist, mock_save,
                          mock_tools_desc, mock_tools, app_client, instance_dir):
        mock_run.return_value = MagicMock(stdout="Salut !", returncode=0)
        with patch.object(dashboard, "CONVERSATION_HISTORY_FILE", instance_dir / "history.jsonl"), \
             patch.object(dashboard, "SOUL_FILE", instance_dir / "soul.md"), \
             patch.object(dashboard, "SUMMARY_FILE", instance_dir / "memory" / "summary.md"), \
             patch.object(dashboard, "JOURNAL_DIR", instance_dir / "journal"):
            resp = app_client.post("/chat/send", data={"message": "hello", "mode": "chat"})
        data = resp.get_json()
        assert data["ok"] is True
        assert data["type"] == "chat"
        assert data["response"] == "Salut !"
        mock_run.assert_called_once()

    @patch("app.dashboard.get_allowed_tools", return_value="")
    @patch("app.dashboard.get_tools_description", return_value="")
    @patch("app.dashboard.save_conversation_message")
    @patch("app.dashboard.load_recent_history", return_value=[])
    @patch("app.dashboard.format_conversation_history", return_value="")
    @patch("app.dashboard.subprocess.run")
    def test_chat_empty_response_fallback(self, mock_run, mock_fmt, mock_hist, mock_save,
                                          mock_tools_desc, mock_tools, app_client, instance_dir):
        mock_run.return_value = MagicMock(stdout="", returncode=0)
        with patch.object(dashboard, "CONVERSATION_HISTORY_FILE", instance_dir / "history.jsonl"), \
             patch.object(dashboard, "SOUL_FILE", instance_dir / "soul.md"), \
             patch.object(dashboard, "SUMMARY_FILE", instance_dir / "memory" / "summary.md"), \
             patch.object(dashboard, "JOURNAL_DIR", instance_dir / "journal"):
            resp = app_client.post("/chat/send", data={"message": "hello", "mode": "chat"})
        data = resp.get_json()
        assert data["ok"] is True
        assert "Try again?" in data["response"]

    @patch("app.dashboard.get_allowed_tools", return_value="")
    @patch("app.dashboard.get_tools_description", return_value="")
    @patch("app.dashboard.save_conversation_message")
    @patch("app.dashboard.load_recent_history", return_value=[])
    @patch("app.dashboard.format_conversation_history", return_value="")
    @patch("app.dashboard.subprocess.run")
    def test_chat_timeout_lite_retry_succeeds(self, mock_run, mock_fmt, mock_hist, mock_save,
                                               mock_tools_desc, mock_tools, app_client, instance_dir):
        """First call times out, lite retry succeeds."""
        mock_run.side_effect = [
            subprocess.TimeoutExpired("claude", 120),
            MagicMock(stdout="Réponse lite !", returncode=0),
        ]
        with patch.object(dashboard, "CONVERSATION_HISTORY_FILE", instance_dir / "history.jsonl"), \
             patch.object(dashboard, "SOUL_FILE", instance_dir / "soul.md"), \
             patch.object(dashboard, "SUMMARY_FILE", instance_dir / "memory" / "summary.md"), \
             patch.object(dashboard, "JOURNAL_DIR", instance_dir / "journal"):
            resp = app_client.post("/chat/send", data={"message": "deep question", "mode": "chat"})
        data = resp.get_json()
        assert data["ok"] is True
        assert data["response"] == "Réponse lite !"
        assert mock_run.call_count == 2

    @patch("app.dashboard.get_allowed_tools", return_value="")
    @patch("app.dashboard.get_tools_description", return_value="")
    @patch("app.dashboard.save_conversation_message")
    @patch("app.dashboard.load_recent_history", return_value=[])
    @patch("app.dashboard.format_conversation_history", return_value="")
    @patch("app.dashboard.subprocess.run")
    def test_chat_timeout_both_attempts(self, mock_run, mock_fmt, mock_hist, mock_save,
                                         mock_tools_desc, mock_tools, app_client, instance_dir):
        """Both full and lite calls time out."""
        mock_run.side_effect = subprocess.TimeoutExpired("claude", 120)
        with patch.object(dashboard, "CONVERSATION_HISTORY_FILE", instance_dir / "history.jsonl"), \
             patch.object(dashboard, "SOUL_FILE", instance_dir / "soul.md"), \
             patch.object(dashboard, "SUMMARY_FILE", instance_dir / "memory" / "summary.md"), \
             patch.object(dashboard, "JOURNAL_DIR", instance_dir / "journal"):
            resp = app_client.post("/chat/send", data={"message": "deep question", "mode": "chat"})
        data = resp.get_json()
        assert data["ok"] is True
        assert "Timeout" in data["response"]
        assert mock_run.call_count == 2

    @patch("app.dashboard.get_allowed_tools", return_value="")
    @patch("app.dashboard.get_tools_description", return_value="")
    @patch("app.dashboard.save_conversation_message")
    @patch("app.dashboard.load_recent_history", return_value=[])
    @patch("app.dashboard.format_conversation_history", return_value="")
    @patch("app.dashboard.subprocess.run")
    def test_chat_timeout_lite_empty_response(self, mock_run, mock_fmt, mock_hist, mock_save,
                                               mock_tools_desc, mock_tools, app_client, instance_dir):
        """First call times out, lite retry returns empty."""
        mock_run.side_effect = [
            subprocess.TimeoutExpired("claude", 120),
            MagicMock(stdout="", returncode=0),
        ]
        with patch.object(dashboard, "CONVERSATION_HISTORY_FILE", instance_dir / "history.jsonl"), \
             patch.object(dashboard, "SOUL_FILE", instance_dir / "soul.md"), \
             patch.object(dashboard, "SUMMARY_FILE", instance_dir / "memory" / "summary.md"), \
             patch.object(dashboard, "JOURNAL_DIR", instance_dir / "journal"):
            resp = app_client.post("/chat/send", data={"message": "deep question", "mode": "chat"})
        data = resp.get_json()
        assert data["ok"] is True
        assert "Timeout" in data["response"]

    @patch("app.dashboard.get_allowed_tools", return_value="")
    @patch("app.dashboard.get_tools_description", return_value="")
    @patch("app.dashboard.save_conversation_message")
    @patch("app.dashboard.load_recent_history", return_value=[])
    @patch("app.dashboard.format_conversation_history", return_value="")
    @patch("app.dashboard.subprocess.run")
    def test_chat_timeout_lite_retry_error(self, mock_run, mock_fmt, mock_hist, mock_save,
                                            mock_tools_desc, mock_tools, app_client, instance_dir):
        """First call times out, lite retry raises OSError."""
        mock_run.side_effect = [
            subprocess.TimeoutExpired("claude", 120),
            OSError("broken"),
        ]
        with patch.object(dashboard, "CONVERSATION_HISTORY_FILE", instance_dir / "history.jsonl"), \
             patch.object(dashboard, "SOUL_FILE", instance_dir / "soul.md"), \
             patch.object(dashboard, "SUMMARY_FILE", instance_dir / "memory" / "summary.md"), \
             patch.object(dashboard, "JOURNAL_DIR", instance_dir / "journal"):
            resp = app_client.post("/chat/send", data={"message": "hi", "mode": "chat"})
        data = resp.get_json()
        assert data["ok"] is False
        assert "broken" in data["error"]

    @patch("app.dashboard.get_allowed_tools", return_value="")
    @patch("app.dashboard.get_tools_description", return_value="")
    @patch("app.dashboard.save_conversation_message")
    @patch("app.dashboard.load_recent_history", return_value=[])
    @patch("app.dashboard.format_conversation_history", return_value="")
    @patch("app.dashboard.subprocess.run")
    def test_chat_exception(self, mock_run, mock_fmt, mock_hist, mock_save,
                            mock_tools_desc, mock_tools, app_client, instance_dir):
        mock_run.side_effect = OSError("claude not found")
        with patch.object(dashboard, "CONVERSATION_HISTORY_FILE", instance_dir / "history.jsonl"), \
             patch.object(dashboard, "SOUL_FILE", instance_dir / "soul.md"), \
             patch.object(dashboard, "SUMMARY_FILE", instance_dir / "memory" / "summary.md"), \
             patch.object(dashboard, "JOURNAL_DIR", instance_dir / "journal"):
            resp = app_client.post("/chat/send", data={"message": "hi", "mode": "chat"})
        data = resp.get_json()
        assert data["ok"] is False
        assert "claude not found" in data["error"]

    @patch("app.dashboard.get_allowed_tools", return_value="")
    @patch("app.dashboard.get_tools_description", return_value="")
    @patch("app.dashboard.save_conversation_message")
    @patch("app.dashboard.load_recent_history", return_value=[])
    @patch("app.dashboard.format_conversation_history", return_value="")
    @patch("app.dashboard.subprocess.run")
    def test_chat_empty_response_logs_stderr(self, mock_run, mock_fmt, mock_hist, mock_save,
                                              mock_tools_desc, mock_tools, app_client, instance_dir, capsys):
        """When Claude returns empty stdout with stderr, stderr should be logged."""
        mock_run.return_value = MagicMock(stdout="", stderr="model overloaded", returncode=1)
        with patch.object(dashboard, "CONVERSATION_HISTORY_FILE", instance_dir / "history.jsonl"), \
             patch.object(dashboard, "SOUL_FILE", instance_dir / "soul.md"), \
             patch.object(dashboard, "SUMMARY_FILE", instance_dir / "memory" / "summary.md"), \
             patch.object(dashboard, "JOURNAL_DIR", instance_dir / "journal"):
            resp = app_client.post("/chat/send", data={"message": "hello", "mode": "chat"})
        data = resp.get_json()
        assert data["ok"] is True
        captured = capsys.readouterr()
        assert "model overloaded" in captured.out

    def test_chat_send_with_project_tag(self, app_client, instance_dir):
        with patch.object(dashboard, "MISSIONS_FILE", instance_dir / "missions.md"):
            resp = app_client.post("/chat/send", data={
                "message": "[project:koan] add feature",
                "mode": "mission",
            })
        data = resp.get_json()
        assert data["ok"] is True
        assert data["type"] == "mission"
        content = (instance_dir / "missions.md").read_text()
        assert "[project:koan] add feature" in content


class TestBuildDashboardPrompt:
    """Test _build_dashboard_prompt lite mode."""

    def test_lite_prompt_strips_journal_and_summary(self, instance_dir):
        with patch.object(dashboard, "CONVERSATION_HISTORY_FILE", instance_dir / "history.jsonl"), \
             patch.object(dashboard, "SOUL_FILE", instance_dir / "soul.md"), \
             patch.object(dashboard, "SUMMARY_FILE", instance_dir / "memory" / "summary.md"), \
             patch.object(dashboard, "INSTANCE_DIR", instance_dir), \
             patch("app.dashboard.load_recent_history", return_value=[]), \
             patch("app.dashboard.format_conversation_history", return_value=""), \
             patch("app.dashboard.get_tools_description", return_value=""):
            prompt = dashboard._build_dashboard_prompt("hello", lite=True)
        assert "Session 1: bootstrapped" not in prompt
        assert "Built the dashboard" not in prompt
        assert "You are Kōan" in prompt


class TestParseProject:
    def test_english_tag(self):
        project, text = dashboard.parse_project("[project:koan] fix bug")
        assert project == "koan"
        assert text == "fix bug"

    def test_french_tag(self):
        project, text = dashboard.parse_project("[projet:koan] fix bug")
        assert project == "koan"
        assert text == "fix bug"

    def test_no_tag(self):
        project, text = dashboard.parse_project("fix bug")
        assert project is None
        assert text == "fix bug"


class TestProgressPage:
    def test_progress_page_renders(self, app_client):
        resp = app_client.get("/progress")
        assert resp.status_code == 200
        assert b"Live Progress" in resp.data
        assert b"EventSource" in resp.data

    def test_progress_page_has_autoscroll(self, app_client):
        resp = app_client.get("/progress")
        assert b"autoscroll" in resp.data


class TestApiProgress:
    def test_no_pending_file(self, app_client):
        resp = app_client.get("/api/progress")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["active"] is False
        assert data["content"] == ""

    def test_with_pending_file(self, app_client, instance_dir):
        pending = instance_dir / "journal" / "pending.md"
        pending.write_text("# Mission: test\n---\n04:26 — started\n")
        resp = app_client.get("/api/progress")
        data = resp.get_json()
        assert data["active"] is True
        assert "Mission: test" in data["content"]
        assert "04:26" in data["content"]


class TestApiProgressStream:
    def test_stream_returns_sse_content_type(self, app_client):
        # Call the view function directly to get the Response object without
        # iterating the infinite SSE generator (which blocks ~14s in the test
        # client waiting for output).
        with dashboard.app.test_request_context("/api/progress/stream"):
            resp = dashboard.api_progress_stream()
        assert resp.content_type == "text/event-stream; charset=utf-8"
        assert resp.headers.get("Cache-Control") == "no-cache"
        assert resp.headers.get("X-Accel-Buffering") == "no"

    def test_stream_sends_initial_event_when_file_exists(self, app_client, instance_dir):
        pending = instance_dir / "journal" / "pending.md"
        pending.write_text("# Mission: live test\n---\n04:30 — doing stuff\n")
        # Patch time.sleep to terminate the generator after the first event.
        # The generator yields a data event (file exists) then hits sleep → error.
        with patch("app.dashboard.time.sleep", side_effect=RuntimeError("break")):
            resp = app_client.get("/api/progress/stream")
        # Read the first chunk from the streaming response
        import json
        data_line = None
        for chunk in resp.response:
            if isinstance(chunk, bytes):
                chunk = chunk.decode()
            if chunk.startswith("data: "):
                data_line = chunk
                break
        assert data_line is not None
        payload = json.loads(data_line[6:].strip())
        assert payload["active"] is True
        assert "live test" in payload["content"]

    def test_stream_sends_inactive_when_no_file(self, app_client, instance_dir):
        # Ensure no pending.md exists
        pending = instance_dir / "journal" / "pending.md"
        if pending.exists():
            pending.unlink()
        # Call the view function directly — we only need to verify the Response
        # metadata, not iterate the generator.
        with dashboard.app.test_request_context("/api/progress/stream"):
            resp = dashboard.api_progress_stream()
        assert "text/event-stream" in resp.content_type


# ---------------------------------------------------------------------------
# Signal status — pause, reason, reset time, daily report
# ---------------------------------------------------------------------------

class TestSignalStatusPause:
    """Test get_signal_status() pause reason parsing and edge cases."""

    def test_pause_signal(self, tmp_path):
        (tmp_path / ".koan-pause").write_text("1")
        with patch.object(dashboard, "KOAN_ROOT", tmp_path):
            status = dashboard.get_signal_status()
            assert status["paused"] is True

    def test_pause_reason_quota(self, tmp_path):
        (tmp_path / ".koan-pause").write_text("quota\n")
        with patch.object(dashboard, "KOAN_ROOT", tmp_path):
            status = dashboard.get_signal_status()
            assert status["pause_reason"] == "quota"

    def test_pause_reason_max_runs(self, tmp_path):
        (tmp_path / ".koan-pause").write_text("max_runs\n")
        with patch.object(dashboard, "KOAN_ROOT", tmp_path):
            status = dashboard.get_signal_status()
            assert status["pause_reason"] == "max_runs"

    def test_pause_reason_with_timestamp_line(self, tmp_path):
        """Pause file with 2 lines: reason + unix timestamp."""
        (tmp_path / ".koan-pause").write_text("quota\n1740000000\n")
        with patch.object(dashboard, "KOAN_ROOT", tmp_path), \
             patch("app.reset_parser.time_until_reset", return_value="2h30m"):
            status = dashboard.get_signal_status()
            assert status["pause_reason"] == "quota"
            assert "2h30m" in status["reset_time"]

    def test_pause_reason_with_three_lines(self, tmp_path):
        """Pause file with human-readable reset on line 3."""
        (tmp_path / ".koan-pause").write_text(
            "quota\n1740000000\nResets at 15:30\n"
        )
        with patch.object(dashboard, "KOAN_ROOT", tmp_path):
            status = dashboard.get_signal_status()
            assert status["reset_time"] == "Resets at 15:30"

    def test_pause_reason_bad_timestamp(self, tmp_path):
        """Non-numeric timestamp — should not crash."""
        (tmp_path / ".koan-pause").write_text("quota\nnot-a-number\n")
        with patch.object(dashboard, "KOAN_ROOT", tmp_path):
            status = dashboard.get_signal_status()
            assert status["pause_reason"] == "quota"
            # reset_time stays empty — ValueError caught silently
            assert status["reset_time"] == ""

    def test_pause_reason_import_error(self, tmp_path):
        """Missing reset_parser module — should not crash."""
        (tmp_path / ".koan-pause").write_text("quota\n1740000000\n")
        with patch.object(dashboard, "KOAN_ROOT", tmp_path), \
             patch.dict("sys.modules", {"app.reset_parser": None}):
            status = dashboard.get_signal_status()
            assert status["pause_reason"] == "quota"

    def test_pause_reason_empty_file(self, tmp_path):
        """Empty pause file (legacy touch-created)."""
        (tmp_path / ".koan-pause").write_text("")
        with patch.object(dashboard, "KOAN_ROOT", tmp_path):
            status = dashboard.get_signal_status()
            assert status["pause_reason"] == ""

    def test_daily_report(self, tmp_path):
        (tmp_path / ".koan-daily-report").write_text("5 sessions, 3 productive")
        with patch.object(dashboard, "KOAN_ROOT", tmp_path):
            status = dashboard.get_signal_status()
            assert status["last_report"] == "5 sessions, 3 productive"

    def test_no_daily_report(self, tmp_path):
        with patch.object(dashboard, "KOAN_ROOT", tmp_path):
            status = dashboard.get_signal_status()
            assert "last_report" not in status

    def test_quota_reset_signal(self, tmp_path):
        (tmp_path / ".koan-quota-reset").write_text("1")
        with patch.object(dashboard, "KOAN_ROOT", tmp_path):
            status = dashboard.get_signal_status()
            assert status["quota_paused"] is True


# ---------------------------------------------------------------------------
# Journal entries — flat files, mixed, limit, filtering
# ---------------------------------------------------------------------------

class TestJournalEntries:
    """Test get_journal_entries() with various directory structures."""

    def test_flat_journal_file(self, tmp_path):
        journal = tmp_path / "journal"
        journal.mkdir()
        (journal / "2026-02-15.md").write_text("Flat journal entry.")
        with patch.object(dashboard, "JOURNAL_DIR", journal):
            entries = dashboard.get_journal_entries()
            assert len(entries) == 1
            assert entries[0]["date"] == "2026-02-15"
            assert entries[0]["entries"][0]["project"] == "general"
            assert "Flat journal entry" in entries[0]["entries"][0]["content"]

    def test_mixed_flat_and_nested(self, tmp_path):
        journal = tmp_path / "journal"
        journal.mkdir()
        (journal / "2026-02-15.md").write_text("Flat entry.")
        nested = journal / "2026-02-16"
        nested.mkdir()
        (nested / "koan.md").write_text("Nested koan entry.")
        with patch.object(dashboard, "JOURNAL_DIR", journal):
            entries = dashboard.get_journal_entries()
            assert len(entries) == 2
            # Most recent first
            assert entries[0]["date"] == "2026-02-16"
            assert entries[1]["date"] == "2026-02-15"

    def test_same_date_flat_and_nested(self, tmp_path):
        """Same date has both flat and nested — both appear."""
        journal = tmp_path / "journal"
        journal.mkdir()
        (journal / "2026-02-15.md").write_text("Flat.")
        nested = journal / "2026-02-15"
        nested.mkdir()
        (nested / "backend.md").write_text("Nested.")
        with patch.object(dashboard, "JOURNAL_DIR", journal):
            entries = dashboard.get_journal_entries()
            assert len(entries) == 1
            # Should have both entries for the same date
            assert len(entries[0]["entries"]) == 2

    def test_limit_parameter(self, tmp_path):
        journal = tmp_path / "journal"
        journal.mkdir()
        for i in range(10):
            d = journal / f"2026-02-{i+1:02d}"
            d.mkdir()
            (d / "koan.md").write_text(f"Entry {i}")
        with patch.object(dashboard, "JOURNAL_DIR", journal):
            entries = dashboard.get_journal_entries(limit=3)
            assert len(entries) == 3
            # Most recent
            assert entries[0]["date"] == "2026-02-10"

    def test_non_date_files_ignored(self, tmp_path):
        journal = tmp_path / "journal"
        journal.mkdir()
        (journal / "pending.md").write_text("not a date")
        (journal / "README.md").write_text("not a date")
        (journal / "2026-02-15").mkdir()
        (journal / "2026-02-15" / "koan.md").write_text("valid")
        with patch.object(dashboard, "JOURNAL_DIR", journal):
            entries = dashboard.get_journal_entries()
            assert len(entries) == 1
            assert entries[0]["date"] == "2026-02-15"

    def test_multiple_projects_in_nested(self, tmp_path):
        journal = tmp_path / "journal"
        d = journal / "2026-02-20"
        d.mkdir(parents=True)
        (d / "koan.md").write_text("Koan work.")
        (d / "backend.md").write_text("Backend work.")
        (d / "tmf.md").write_text("TMF work.")
        with patch.object(dashboard, "JOURNAL_DIR", journal):
            entries = dashboard.get_journal_entries()
            assert len(entries) == 1
            projects = [e["project"] for e in entries[0]["entries"]]
            assert "koan" in projects
            assert "backend" in projects
            assert "tmf" in projects


# ---------------------------------------------------------------------------
# Index route — state determination
# ---------------------------------------------------------------------------

class TestIndexState:
    """Test index route state label logic."""

    def test_stopped_state(self, app_client, tmp_path):
        (tmp_path / ".koan-stop").write_text("1")
        resp = app_client.get("/")
        assert resp.status_code == 200
        assert b"Stopped" in resp.data

    def test_quota_paused_state(self, app_client, tmp_path):
        (tmp_path / ".koan-quota-reset").write_text("1")
        resp = app_client.get("/")
        assert resp.status_code == 200
        assert b"quota" in resp.data

    def test_running_state_with_loop(self, app_client, tmp_path):
        (tmp_path / ".koan-status").write_text("5/20")
        resp = app_client.get("/")
        assert resp.status_code == 200
        assert b"5/20" in resp.data

    def test_idle_state(self, app_client, tmp_path):
        """No signal files at all — idle."""
        resp = app_client.get("/")
        assert resp.status_code == 200
        assert b"Idle" in resp.data

    def test_stop_takes_precedence(self, app_client, tmp_path):
        """Stop + quota → shows Stopped."""
        (tmp_path / ".koan-stop").write_text("1")
        (tmp_path / ".koan-quota-reset").write_text("1")
        (tmp_path / ".koan-status").write_text("3/20")
        resp = app_client.get("/")
        assert resp.status_code == 200
        assert b"Stopped" in resp.data


# ---------------------------------------------------------------------------
# Add mission edge cases
# ---------------------------------------------------------------------------

class TestAddMissionEdges:
    def test_add_empty_mission_redirects(self, app_client, instance_dir):
        """Empty text should redirect without modifying missions."""
        original = (instance_dir / "missions.md").read_text()
        resp = app_client.post("/missions/add", data={
            "mission": "",
            "project": "koan",
        })
        assert resp.status_code == 302  # redirect
        assert (instance_dir / "missions.md").read_text() == original

    def test_add_whitespace_only_mission(self, app_client, instance_dir):
        """Whitespace-only text should redirect without modifying missions."""
        original = (instance_dir / "missions.md").read_text()
        resp = app_client.post("/missions/add", data={
            "mission": "   ",
            "project": "",
        })
        assert resp.status_code == 302
        assert (instance_dir / "missions.md").read_text() == original


# ---------------------------------------------------------------------------
# read_file helper
# ---------------------------------------------------------------------------

class TestReadFile:
    def test_existing_file(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("hello world")
        assert dashboard.read_file(f) == "hello world"

    def test_missing_file(self, tmp_path):
        assert dashboard.read_file(tmp_path / "missing.md") == ""


# ---------------------------------------------------------------------------
# _build_dashboard_prompt — full mode
# ---------------------------------------------------------------------------

class TestBuildDashboardPromptFull:
    """Test _build_dashboard_prompt in full (default) mode."""

    def test_full_prompt_includes_journal_and_summary(self, instance_dir):
        with patch.object(dashboard, "CONVERSATION_HISTORY_FILE", instance_dir / "history.jsonl"), \
             patch.object(dashboard, "SOUL_FILE", instance_dir / "soul.md"), \
             patch.object(dashboard, "SUMMARY_FILE", instance_dir / "memory" / "summary.md"), \
             patch.object(dashboard, "INSTANCE_DIR", instance_dir), \
             patch("app.dashboard.load_recent_history", return_value=[]), \
             patch("app.dashboard.format_conversation_history", return_value=""), \
             patch("app.dashboard.get_tools_description", return_value=""):
            prompt = dashboard._build_dashboard_prompt("hello", lite=False)
        assert "You are Kōan" in prompt
        assert "Session 1: bootstrapped" in prompt

    def test_full_prompt_truncates_summary(self, instance_dir):
        """Summary > 1500 chars should be truncated."""
        long_summary = "A" * 3000
        (instance_dir / "memory" / "summary.md").write_text(long_summary)
        with patch.object(dashboard, "CONVERSATION_HISTORY_FILE", instance_dir / "history.jsonl"), \
             patch.object(dashboard, "SOUL_FILE", instance_dir / "soul.md"), \
             patch.object(dashboard, "SUMMARY_FILE", instance_dir / "memory" / "summary.md"), \
             patch.object(dashboard, "INSTANCE_DIR", instance_dir), \
             patch("app.dashboard.load_recent_history", return_value=[]), \
             patch("app.dashboard.format_conversation_history", return_value=""), \
             patch("app.dashboard.get_tools_description", return_value=""):
            prompt = dashboard._build_dashboard_prompt("hello")
        # Full 3000-char summary should not appear
        assert "A" * 3000 not in prompt
        # But truncated version should
        assert "A" * 1500 in prompt


# ---------------------------------------------------------------------------
# API status response structure
# ---------------------------------------------------------------------------

class TestApiStatusStructure:
    def test_api_status_has_signals(self, app_client, tmp_path):
        (tmp_path / ".koan-pause").write_text("1")
        resp = app_client.get("/api/status")
        data = resp.get_json()
        assert "signals" in data
        assert data["signals"]["paused"] is True

    def test_api_status_done_count(self, app_client):
        resp = app_client.get("/api/status")
        data = resp.get_json()
        assert data["missions"]["done"] == 1


# ---------------------------------------------------------------------------
# Project filtering
# ---------------------------------------------------------------------------

class TestProjectFiltering:
    """Test ?project= filtering on routes and /api/projects."""

    def test_api_projects(self, app_client):
        with patch("app.dashboard.get_known_projects", return_value=[("koan", "/p/koan")]):
            resp = app_client.get("/api/projects")
        data = resp.get_json()
        assert "projects" in data
        assert "koan" in data["projects"]

    def test_api_projects_includes_mission_tags(self, app_client, instance_dir):
        """Projects from mission tags are included even if not in config."""
        with patch("app.dashboard.get_known_projects", return_value=[]):
            resp = app_client.get("/api/projects")
        data = resp.get_json()
        # missions.md has [project:koan] tag
        assert "koan" in data["projects"]

    def test_missions_filtered_by_project(self, app_client):
        resp = app_client.get("/missions?project=koan")
        assert resp.status_code == 200
        assert b"Build dashboard" in resp.data
        assert b"Fix something" not in resp.data

    def test_missions_unfiltered(self, app_client):
        resp = app_client.get("/missions")
        assert resp.status_code == 200
        assert b"Build dashboard" in resp.data
        assert b"Fix something" in resp.data

    def test_index_filtered_by_project(self, app_client):
        resp = app_client.get("/?project=koan")
        assert resp.status_code == 200
        assert b"Build dashboard" in resp.data

    def test_journal_filtered_by_project(self, app_client):
        resp = app_client.get("/journal?project=koan")
        assert resp.status_code == 200
        assert b"Built the dashboard" in resp.data

    def test_journal_filtered_no_match(self, app_client):
        resp = app_client.get("/journal?project=nonexistent")
        assert resp.status_code == 200
        assert b"Built the dashboard" not in resp.data

    def test_filter_missions_helper(self):
        missions = {
            "pending": [
                "- [project:koan] Task A",
                "- [project:other] Task B",
                "- Task C",
            ],
            "in_progress": [],
            "done": [],
        }
        filtered = dashboard._filter_missions_by_project(missions, "koan")
        assert len(filtered["pending"]) == 1
        assert "Task A" in filtered["pending"][0]

    def test_filter_missions_empty_project(self):
        missions = {"pending": ["- Task A", "- Task B"], "in_progress": [], "done": []}
        filtered = dashboard._filter_missions_by_project(missions, "")
        assert filtered == missions

    def test_project_badge_filter(self):
        assert "koan" in dashboard.project_badge_filter("- [project:koan] Fix bug")
        assert dashboard.project_badge_filter("- Fix bug") == ""

    def test_strip_project_tag_filter(self):
        assert dashboard.strip_project_tag_filter("- [project:koan] Fix bug") == "- Fix bug"
        assert dashboard.strip_project_tag_filter("- Fix bug") == "- Fix bug"


# ---------------------------------------------------------------------------
# Mission queue API endpoints
# ---------------------------------------------------------------------------

class TestApiMissions:
    """Test GET /api/missions."""

    def test_returns_sections(self, app_client):
        resp = app_client.get("/api/missions")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "pending" in data
        assert "in_progress" in data
        assert "done" in data
        assert len(data["pending"]) == 2


class TestApiMissionsReorder:
    """Test POST /api/missions/reorder."""

    def test_valid_reorder(self, app_client, instance_dir):
        with patch.object(dashboard, "MISSIONS_FILE", instance_dir / "missions.md"):
            resp = app_client.post("/api/missions/reorder",
                json={"position": 2, "target": 1})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert "pending" in data

    def test_invalid_position(self, app_client, instance_dir):
        with patch.object(dashboard, "MISSIONS_FILE", instance_dir / "missions.md"):
            resp = app_client.post("/api/missions/reorder",
                json={"position": 99, "target": 1})
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["ok"] is False

    def test_missing_params(self, app_client):
        resp = app_client.post("/api/missions/reorder", json={"position": 1})
        assert resp.status_code == 400

    def test_same_position(self, app_client, instance_dir):
        with patch.object(dashboard, "MISSIONS_FILE", instance_dir / "missions.md"):
            resp = app_client.post("/api/missions/reorder",
                json={"position": 1, "target": 1})
        assert resp.status_code == 400


class TestApiMissionsCancel:
    """Test POST /api/missions/cancel."""

    def test_valid_cancel(self, app_client, instance_dir):
        with patch.object(dashboard, "MISSIONS_FILE", instance_dir / "missions.md"):
            resp = app_client.post("/api/missions/cancel",
                json={"position": 1})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert "cancelled" in data
        assert "pending" in data

    def test_invalid_position(self, app_client, instance_dir):
        with patch.object(dashboard, "MISSIONS_FILE", instance_dir / "missions.md"):
            resp = app_client.post("/api/missions/cancel",
                json={"position": 99})
        assert resp.status_code == 400

    def test_missing_position(self, app_client):
        resp = app_client.post("/api/missions/cancel", json={})
        assert resp.status_code == 400


class TestApiMissionsEdit:
    """Test POST /api/missions/edit."""

    def test_valid_edit(self, app_client, instance_dir):
        with patch.object(dashboard, "MISSIONS_FILE", instance_dir / "missions.md"):
            resp = app_client.post("/api/missions/edit",
                json={"position": 1, "text": "Updated mission"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        content = (instance_dir / "missions.md").read_text()
        assert "Updated mission" in content

    def test_empty_text(self, app_client, instance_dir):
        with patch.object(dashboard, "MISSIONS_FILE", instance_dir / "missions.md"):
            resp = app_client.post("/api/missions/edit",
                json={"position": 1, "text": ""})
        assert resp.status_code == 400

    def test_invalid_position(self, app_client, instance_dir):
        with patch.object(dashboard, "MISSIONS_FILE", instance_dir / "missions.md"):
            resp = app_client.post("/api/missions/edit",
                json={"position": 99, "text": "New text"})
        assert resp.status_code == 400

    def test_missing_position(self, app_client):
        resp = app_client.post("/api/missions/edit",
            json={"text": "Some text"})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Plans — _parse_plan_progress unit tests
# ---------------------------------------------------------------------------

class TestParsePlanProgress:
    """Unit tests for _parse_plan_progress()."""

    _STANDARD_PLAN = """
#### Phase 1: Backend endpoints

- What: add routes
- Done when: routes return JSON

#### Phase 2: Frontend

- What: build HTML page
- Done when: page renders

#### Phase 3: Tests

✅ Written and passing
"""

    _COMPLETED_PHASE = """
#### Phase 1: Setup

✅ Environment configured
"""

    _CHECKBOX_PHASE = """
#### Phase 1: Analysis

- [x] Read codebase
- [x] Understand structure
"""

    _DONE_TEXT_PHASE = """
#### Phase 1: Research

Done — findings documented
"""

    def test_extracts_phases(self):
        result = dashboard._parse_plan_progress(self._STANDARD_PLAN)
        assert result["total"] == 3
        assert result["phases"][0]["title"] == "Backend endpoints"
        assert result["phases"][1]["title"] == "Frontend"
        assert result["phases"][2]["title"] == "Tests"

    def test_detects_checkmark_completion(self):
        result = dashboard._parse_plan_progress(self._COMPLETED_PHASE)
        assert result["total"] == 1
        assert result["completed"] == 1
        assert result["phases"][0]["completed"] is True
        assert result["percent"] == 100

    def test_detects_checkbox_completion(self):
        result = dashboard._parse_plan_progress(self._CHECKBOX_PHASE)
        assert result["phases"][0]["completed"] is True

    def test_detects_done_text_completion(self):
        result = dashboard._parse_plan_progress(self._DONE_TEXT_PHASE)
        assert result["phases"][0]["completed"] is True

    def test_incomplete_phases(self):
        result = dashboard._parse_plan_progress(self._STANDARD_PLAN)
        # Phase 1 and 2 have no completion markers; Phase 3 has ✅
        assert result["phases"][0]["completed"] is False
        assert result["phases"][1]["completed"] is False
        assert result["phases"][2]["completed"] is True
        assert result["completed"] == 1
        assert result["percent"] == 33

    def test_empty_markdown(self):
        result = dashboard._parse_plan_progress("")
        assert result == {"phases": [], "completed": 0, "total": 0, "percent": 0}

    def test_no_phases(self):
        result = dashboard._parse_plan_progress("# Some title\n\nNo phases here.")
        assert result["total"] == 0
        assert result["percent"] == 0

    def test_malformed_plan_best_effort(self):
        """Plans that don't follow the strict format return gracefully."""
        result = dashboard._parse_plan_progress("Random content\nwithout phases")
        assert result["total"] == 0


# ---------------------------------------------------------------------------
# Plans — API endpoint tests
# ---------------------------------------------------------------------------

class TestPlansPage:
    """Tests for /plans page and /api/plans* endpoints."""

    def test_plans_page_renders(self, app_client):
        resp = app_client.get("/plans")
        assert resp.status_code == 200
        assert b"Plans" in resp.data

    def test_api_plans_no_projects(self, app_client):
        """When no projects are configured, returns empty plans list."""
        with patch("app.utils.get_known_projects", return_value=[]):
            resp = app_client.get("/api/plans")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["plans"] == []

    def test_api_plans_skips_projects_without_github_url(self, app_client):
        with patch("app.utils.get_known_projects", return_value=[("myproject", "/some/path")]), \
             patch("app.dashboard._get_project_repo", return_value=None):
            resp = app_client.get("/api/plans")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["plans"] == []

    def test_api_plans_returns_plan_issues(self, app_client):
        gh_response = json.dumps([{
            "number": 42,
            "title": "Feature X plan",
            "state": "open",
            "body": "#### Phase 1: Setup\n\nDo setup.\n\n#### Phase 2: Implement\n\n✅ Done",
            "updatedAt": "2026-03-14T10:00:00Z",
            "url": "https://github.com/owner/repo/issues/42",
        }])
        with patch("app.utils.get_known_projects", return_value=[("myproject", "/path")]), \
             patch("app.dashboard._get_project_repo", return_value="owner/repo"), \
             patch.dict("app.dashboard._plans_cache", {}, clear=True), \
             patch("app.github.run_gh", return_value=gh_response):
            resp = app_client.get("/api/plans")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["plans"]) == 1
        plan = data["plans"][0]
        assert plan["number"] == 42
        assert plan["title"] == "Feature X plan"
        assert plan["project"] == "myproject"
        assert plan["progress"]["total"] == 2
        assert plan["progress"]["completed"] == 1

    def test_api_plans_project_filter(self, app_client):
        """Project filter limits results to matching project."""
        with patch("app.utils.get_known_projects",
                   return_value=[("proj_a", "/a"), ("proj_b", "/b")]), \
             patch("app.dashboard._get_project_repo", return_value="owner/repo"), \
             patch.dict("app.dashboard._plans_cache", {}, clear=True), \
             patch("app.github.run_gh", return_value="[]"):
            resp = app_client.get("/api/plans?project=proj_a")
        assert resp.status_code == 200
        # Only proj_a was queried (proj_b skipped by filter)

    def test_api_plans_force_refresh_bypasses_cache(self, app_client):
        """force=1 query param bypasses the server-side cache."""
        cached_plan = [{
            "number": 1, "title": "Cached", "state": "open", "body": "",
            "url": "", "updatedAt": "", "progress": {"phases": [], "completed": 0, "total": 0, "percent": 0},
            "project": "myproject", "repo": "owner/repo",
        }]
        import time as _time
        fresh_cache = {"plans:myproject": (_time.time(), cached_plan)}

        fresh_gh = json.dumps([{
            "number": 2, "title": "Fresh", "state": "open",
            "body": "", "updatedAt": "", "url": "",
        }])
        with patch("app.utils.get_known_projects", return_value=[("myproject", "/p")]), \
             patch("app.dashboard._get_project_repo", return_value="owner/repo"), \
             patch.dict("app.dashboard._plans_cache", fresh_cache, clear=True), \
             patch("app.github.run_gh", return_value=fresh_gh):
            # Without force — should use cache
            resp = app_client.get("/api/plans")
            data = resp.get_json()
            assert data["plans"][0]["title"] == "Cached"

            # With force=1 — should bypass cache and fetch fresh
            resp = app_client.get("/api/plans?force=1")
            data = resp.get_json()
            assert data["plans"][0]["title"] == "Fresh"

    def test_api_plan_detail_no_github_url(self, app_client):
        with patch("app.dashboard._get_project_repo", return_value=None):
            resp = app_client.get("/api/plans/myproject/42")
        assert resp.status_code == 404

    def test_api_plan_detail_returns_structure(self, app_client):
        gh_response = json.dumps({
            "number": 42,
            "title": "Feature X plan",
            "state": "open",
            "body": "#### Phase 1: Setup\n\nDo setup.",
            "url": "https://github.com/owner/repo/issues/42",
            "updatedAt": "2026-03-14T10:00:00Z",
            "comments": [
                {"body": "#### Phase 1: Setup\n\n✅ Done.", "createdAt": "2026-03-14T11:00:00Z"}
            ],
        })
        with patch("app.dashboard._get_project_repo", return_value="owner/repo"), \
             patch("app.github.run_gh", return_value=gh_response), \
             patch.object(dashboard, "MISSIONS_FILE", Path("/nonexistent/missions.md")):
            resp = app_client.get("/api/plans/myproject/42")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["number"] == 42
        assert data["title"] == "Feature X plan"
        assert len(data["comments"]) == 1
        # latest_body should be the last comment
        assert "✅ Done" in data["latest_body"]
        assert data["progress"]["completed"] == 1

    def test_find_linked_missions(self, instance_dir):
        """_find_linked_missions finds missions that reference an issue URL."""
        missions_file = instance_dir / "missions.md"
        missions_file.write_text(
            "## Pending\n\n"
            "- /plan https://github.com/owner/repo/issues/42\n"
            "- Some unrelated mission\n"
        )
        with patch.object(dashboard, "MISSIONS_FILE", missions_file):
            linked = dashboard._find_linked_missions(
                "https://github.com/owner/repo/issues/42", 42
            )
        assert len(linked) == 1
        assert "/plan" in linked[0]


# ---------------------------------------------------------------------------
# Automation rules routes
# ---------------------------------------------------------------------------

import yaml as _yaml


class TestRulesRoutes:
    """Integration tests for the /api/rules and /rules endpoints."""

    def test_get_rules_empty(self, app_client, instance_dir):
        with patch.object(dashboard, "INSTANCE_DIR", instance_dir):
            resp = app_client.get("/api/rules")
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_post_rule_creates_entry(self, app_client, instance_dir):
        with patch.object(dashboard, "INSTANCE_DIR", instance_dir):
            resp = app_client.post("/api/rules", json={
                "event": "post_mission",
                "action": "notify",
                "params": {"message": "done"},
            })
            assert resp.status_code == 201
            rule = resp.get_json()
            assert rule["event"] == "post_mission"
            assert rule["action"] == "notify"
            assert rule["params"]["message"] == "done"

            # Appears in subsequent GET
            resp2 = app_client.get("/api/rules")
            assert resp2.status_code == 200
            rules = resp2.get_json()
            assert len(rules) == 1
            assert rules[0]["id"] == rule["id"]

    def test_post_rule_unknown_event_returns_400(self, app_client, instance_dir):
        with patch.object(dashboard, "INSTANCE_DIR", instance_dir):
            resp = app_client.post("/api/rules", json={
                "event": "no_such_event",
                "action": "notify",
            })
        assert resp.status_code == 400
        assert "error" in resp.get_json()

    def test_post_rule_unknown_action_returns_400(self, app_client, instance_dir):
        with patch.object(dashboard, "INSTANCE_DIR", instance_dir):
            resp = app_client.post("/api/rules", json={
                "event": "post_mission",
                "action": "send_email",
            })
        assert resp.status_code == 400

    def test_patch_rule_toggles_enabled(self, app_client, instance_dir):
        with patch.object(dashboard, "INSTANCE_DIR", instance_dir):
            create = app_client.post("/api/rules", json={
                "event": "post_mission",
                "action": "notify",
                "params": {"message": "hi"},
            })
            rule_id = create.get_json()["id"]
            assert create.get_json()["enabled"] is True

            patch_resp = app_client.patch(f"/api/rules/{rule_id}", json={"enabled": False})
            assert patch_resp.status_code == 200
            assert patch_resp.get_json()["enabled"] is False

    def test_delete_rule_removes_it(self, app_client, instance_dir):
        with patch.object(dashboard, "INSTANCE_DIR", instance_dir):
            create = app_client.post("/api/rules", json={
                "event": "pre_mission",
                "action": "pause",
            })
            rule_id = create.get_json()["id"]

            del_resp = app_client.delete(f"/api/rules/{rule_id}")
            assert del_resp.status_code == 200

            rules = app_client.get("/api/rules").get_json()
            assert all(r["id"] != rule_id for r in rules)

    def test_delete_nonexistent_rule_returns_404(self, app_client, instance_dir):
        with patch.object(dashboard, "INSTANCE_DIR", instance_dir):
            resp = app_client.delete("/api/rules/does_not_exist")
        assert resp.status_code == 404

    def test_rules_page_renders_without_error(self, app_client, instance_dir):
        with patch.object(dashboard, "INSTANCE_DIR", instance_dir), \
             patch.object(dashboard, "JOURNAL_DIR", instance_dir / "journal"):
            resp = app_client.get("/rules")
        assert resp.status_code == 200
        assert b"Automation Rules" in resp.data

    def test_rules_page_shows_empty_state_when_no_rules(self, app_client, instance_dir):
        with patch.object(dashboard, "INSTANCE_DIR", instance_dir), \
             patch.object(dashboard, "JOURNAL_DIR", instance_dir / "journal"):
            resp = app_client.get("/rules")
        assert resp.status_code == 200
        assert b"No rules yet" in resp.data
