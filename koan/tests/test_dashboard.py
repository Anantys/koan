"""Tests for koan/dashboard.py"""

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
        assert b"Quota" in resp.data

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
